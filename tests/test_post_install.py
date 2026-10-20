"""``netkeep setup`` (#684, ADR 0011 rules 6 and 7): the login item, the wait for ``serve``,
the first-run page, a re-run that changes nothing, and ``--remove``.

Everything is a fake: ``launchctl`` (the agents directory is under ``tmp_path``), the Chrome
launcher, the health check (a loopback server on a free port where it is real) and the
data directory. Nothing here touches the real ``~/Library/LaunchAgents`` or starts Chrome.
"""

from __future__ import annotations

import json
import os
import plistlib
import socket
import threading
from collections.abc import Iterator, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from typer.testing import CliRunner

from netkeep import cli, paths
from netkeep.services import local_api_key, login_item, post_install
from netkeep.services import posture as posture_service
from netkeep.services.chrome_launch import LaunchResult, LaunchStatus, Refusal
from netkeep.services.login_item import LoginItem, RunResult

UID = 501


class FakeLaunchctl:
    """Remembers which labels launchd has loaded and disabled; ``fail`` fails one verb."""

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

    def changes(self) -> list[str]:
        """The verbs that change launchd, as opposed to reading it."""
        return [call[0] for call in self.calls if call[0] not in ("print", "print-disabled")]


class FakeLauncher:
    """Stands in for ``netkeep open``'s launcher: records each start's ``first_run``."""

    def __init__(self, result: LaunchResult) -> None:
        self.result = result
        self.starts: list[bool] = []
        self.built: list[tuple[str, str, str]] = []

    def factory(self, cdp_url: str, base: str, key: str) -> FakeLauncher:
        self.built.append((cdp_url, base, key))
        return self

    async def start(self, *, first_run: bool = False) -> LaunchResult:
        self.starts.append(first_run)
        return self.result


class Machine:
    """The fakes one ``netkeep setup`` runs against, wired into the CLI."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.launchctl = FakeLaunchctl()
        self.data = paths.data_dir()  # conftest's scratch NETKEEP_DATA under tmp_path
        self.agents = tmp_path / "LaunchAgents"
        self.executable = tmp_path / "bin" / "netkeep"
        self.executable.parent.mkdir()
        self.executable.write_text("#!/bin/sh\n")
        self.executable.chmod(0o755)
        self.lock_held = False
        self.answers = True
        """Whether ``serve`` answers its health check once launchd has the agent loaded."""
        self.health_asked: list[str] = []
        self.chrome_pid: int | None = None
        self.launcher = FakeLauncher(LaunchResult(LaunchStatus.STARTED, "started it", pid=4242))
        self.tool_shim: Path | None = self.executable
        monkeypatch.setattr(login_item, "LoginItem", self.item)
        monkeypatch.setattr(login_item, "tool_shim", lambda _env, _beside: self.tool_shim)
        monkeypatch.setattr(post_install, "serve_answers", self.serve_answers)
        monkeypatch.setattr(post_install, "_POLL_S", 0.0)
        monkeypatch.setattr(posture_service, "netkeep_chrome_pid", lambda: self.chrome_pid)
        monkeypatch.setattr(cli, "_open_launcher", self.launcher.factory)
        monkeypatch.setattr(local_api_key, "stored_key", lambda: "fake-local-key")

    def item(self) -> LoginItem:
        return LoginItem(
            data=self.data,
            agents_dir=self.agents,
            runner=self.launchctl,
            uid=UID,
            executable=self.executable,
            macos=True,
            environ={},
            lock_held=lambda: self.lock_held,
            sleep=lambda _s: None,
        )

    def serve_answers(self, base: str) -> bool:
        self.health_asked.append(base)
        return self.answers and login_item.label_for(self.data) in self.launchctl.loaded

    @property
    def plist(self) -> Path:
        return self.agents / f"{login_item.label_for(self.data)}.plist"

    def setup(self, *args: str) -> tuple[int, str]:
        result = CliRunner().invoke(cli.app, ["setup", "--wait", "0", *args])
        return result.exit_code, result.output


@pytest.fixture
def machine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Machine:
    return Machine(monkeypatch, tmp_path)


# --- a fresh setup, a re-run, --remove ------------------------------------------------------


def test_a_fresh_setup_turns_on_the_login_item_waits_for_serve_and_opens_the_first_run_page(
    machine: Machine,
) -> None:
    code, output = machine.setup()

    assert code == 0, output
    document = plistlib.loads(machine.plist.read_bytes())
    assert document["ProgramArguments"] == [str(machine.executable), "serve"]
    assert document["EnvironmentVariables"][paths.DATA_ENV] == str(machine.data)
    assert machine.launchctl.changes() == ["enable", "bootstrap"]
    assert machine.health_asked == ["http://127.0.0.1:8000"]
    assert machine.launcher.starts == [True]  # the first-run page, once
    assert machine.launcher.built[0][1:] == ("http://127.0.0.1:8000", "fake-local-key")
    assert output.count("   done: ") == 3
    assert "netkeep is set up." in output
    assert "note:" not in output  # a uv tool install says nothing about its path


def test_a_re_run_reports_whats_done_and_changes_nothing(machine: Machine) -> None:
    assert machine.setup()[0] == 0
    before = machine.plist.read_bytes()
    machine.launchctl.calls.clear()
    machine.chrome_pid = 4242  # the first run's Chrome is still up

    code, output = machine.setup()

    assert code == 0, output
    assert machine.launchctl.changes() == []  # no enable, bootstrap or bootout: serve keeps running
    assert machine.plist.read_bytes() == before
    assert machine.launcher.starts == [True]  # no second window
    assert output.count("   already done: ") == 2
    assert "run netkeep open" in output


def test_a_login_item_that_is_on_but_not_loaded_is_started(machine: Machine) -> None:
    assert machine.setup()[0] == 0
    machine.launchctl.loaded.clear()  # say, booted out by hand
    machine.launchctl.calls.clear()

    code, output = machine.setup()

    assert code == 0, output
    assert machine.launchctl.changes() == ["enable", "bootstrap"]


def test_a_login_item_for_another_netkeep_is_repointed(machine: Machine, tmp_path: Path) -> None:
    other = tmp_path / "old-checkout" / ".venv" / "bin" / "netkeep"
    machine.agents.mkdir()
    machine.plist.write_bytes(
        plistlib.dumps(
            {"Label": login_item.label_for(machine.data), "ProgramArguments": [str(other), "serve"]}
        )
    )
    machine.launchctl.loaded.add(login_item.label_for(machine.data))

    code, output = machine.setup()

    assert code == 0, output
    assert plistlib.loads(machine.plist.read_bytes())["ProgramArguments"][0] == str(
        machine.executable
    )
    assert "bootout" in machine.launchctl.changes()


def test_a_port_the_login_item_was_given_is_kept_and_polled(machine: Machine) -> None:
    machine.agents.mkdir()
    machine.plist.write_bytes(
        plistlib.dumps(
            {
                "Label": login_item.label_for(machine.data),
                "ProgramArguments": [str(machine.executable), "serve", "--port", "8123"],
            }
        )
    )

    code, output = machine.setup()

    assert code == 0, output
    assert plistlib.loads(machine.plist.read_bytes())["ProgramArguments"][-2:] == ["--port", "8123"]
    assert machine.health_asked == ["http://127.0.0.1:8123"]
    assert machine.launcher.built[0][1] == "http://127.0.0.1:8123"


def test_remove_turns_the_login_item_off_and_keeps_the_data(machine: Machine) -> None:
    assert machine.setup()[0] == 0
    kept = machine.data / "kept.txt"
    kept.write_text("still here")
    logs = sorted(p.name for p in (machine.data / "logs").iterdir())

    code, output = machine.setup("--remove")

    assert code == 0, output
    assert not machine.plist.exists()
    assert machine.launchctl.loaded == set()
    assert kept.read_text() == "still here"
    assert sorted(p.name for p in (machine.data / "logs").iterdir()) == logs
    assert "   done: Turned off." in output and "Nothing was deleted." in output
    assert machine.launcher.starts == [True]  # --remove opens nothing

    machine.launchctl.calls.clear()
    code, output = machine.setup("--remove")
    assert code == 0, output
    assert "   already done: " in output
    assert machine.launchctl.changes() == []


def test_setup_never_chooses_or_moves_the_data_directory(machine: Machine) -> None:
    """Rule 7: the plist names the data directory in use, and the default one under the
    (scratch) home is never made."""
    assert machine.setup()[0] == 0
    document = plistlib.loads(machine.plist.read_bytes())
    assert document["EnvironmentVariables"][paths.DATA_ENV] == str(machine.data)
    assert not paths.default_data_dir().exists()


# --- each step failing -----------------------------------------------------------------------


def test_serve_not_answering_fails_with_a_fix_and_opens_nothing(machine: Machine) -> None:
    machine.answers = False

    code, output = machine.setup()

    assert code == 1
    assert "failed: netkeep serve didn't answer at http://127.0.0.1:8000/api/v1/health" in output
    assert f"to fix it: Look in {machine.data / 'logs' / 'serve.err.log'}" in output
    assert machine.launcher.starts == []
    assert "netkeep is set up." not in output


def test_the_wait_polls_until_serve_answers(machine: Machine) -> None:
    item = machine.item()
    item.enable()
    answers = iter([False, False, True])
    now = iter([0.0, 0.0, 1.0, 2.0, 3.0])
    slept: list[float] = []
    seams = post_install.Seams(
        base_url=lambda _host, _port: "http://127.0.0.1:8123",
        open_first_run=lambda _base: LaunchResult(LaunchStatus.HANDED_OFF, "handed off"),
        chrome_pid=lambda: None,
        serve_answers=lambda _base: next(answers),
        sleep=slept.append,
        clock=lambda: next(now),
    )
    lines: list[str] = []

    assert post_install.run(item, seams, lines.append, tool_install=True, wait_s=10) == 0
    assert len(slept) == 2
    assert "   done: netkeep serve answers." in lines


def test_the_launcher_refusing_fails_with_its_fix(machine: Machine) -> None:
    machine.launcher.result = LaunchResult(
        LaunchStatus.REFUSED, "the Chrome on port 9444 isn't netkeep's", Refusal.NOT_DEDICATED
    )

    code, output = machine.setup()

    assert code == 1
    assert "failed: netkeep's Chrome: the Chrome on port 9444 isn't netkeep's." in output
    assert "to fix it: Another Chrome holds netkeep's debugging port." in output
    assert machine.plist.exists()  # the login item stays on; a re-run picks up from here


def test_a_failed_launch_without_a_refusal_still_says_what_to_do(machine: Machine) -> None:
    machine.launcher.result = LaunchResult(LaunchStatus.FAILED, "Chrome exited with status 1")

    code, output = machine.setup()

    assert code == 1
    assert "to fix it: Fix what it says, then run netkeep setup again" in output


def test_an_unconfirmed_hand_off_counts_but_says_how_to_open_it(machine: Machine) -> None:
    machine.launcher.result = LaunchResult(LaunchStatus.HANDOFF_UNCONFIRMED, "probably opened")

    code, output = machine.setup()

    assert code == 0, output
    assert "If no netkeep window opened, run netkeep open." in output


def test_every_refusal_has_a_fix() -> None:
    assert set(post_install.REFUSAL_FIXES) == set(Refusal)


def test_launchctl_failing_fails_the_first_step(machine: Machine) -> None:
    machine.launchctl.fail = "bootstrap"

    code, output = machine.setup()

    assert code == 1
    assert "failed: launchctl bootstrap failed: Input/output error" in output
    assert "to fix it: run netkeep login-item status" in output
    assert not machine.plist.exists()  # nothing half-installed
    assert machine.health_asked == [] and machine.launcher.starts == []


def test_off_macos_setup_fails_and_says_why(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def linux_item() -> LoginItem:
        return LoginItem(
            data=machine.data, agents_dir=machine.agents, runner=machine.launchctl, macos=False
        )

    monkeypatch.setattr(login_item, "LoginItem", linux_item)

    code, output = machine.setup()

    assert code == 1
    assert "failed: Run at login uses launchd, which only macOS has." in output
    assert machine.launchctl.calls == []


# --- the executable: uv's shim, or the checkout's .venv --------------------------------------


def test_a_checkout_venv_is_used_but_setup_says_so(machine: Machine) -> None:
    machine.tool_shim = None

    code, output = machine.setup()

    assert code == 0, output
    assert f"note: this netkeep runs from {machine.executable}, not from a uv tool install" in (
        output
    )


@pytest.fixture
def tool_env(tmp_path: Path) -> Iterator[tuple[Path, Path, Path]]:
    """A uv tool environment and uv's bin folder, both under ``tmp_path``: the environment,
    its interpreter, and the shim uv linked into the bin folder."""
    environment = tmp_path / "uv" / "tools" / "netkeep"
    (environment / "bin").mkdir(parents=True)
    python = environment / "bin" / "python"
    python.write_text("")
    inside = environment / "bin" / "netkeep"
    inside.write_text("#!/bin/sh\n")
    inside.chmod(0o755)
    bin_dir = tmp_path / "home" / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    shim = bin_dir / "netkeep"
    shim.symlink_to(inside)
    (environment / login_item.UV_RECEIPT).write_text(
        "[tool]\n"
        'requirements = [{ name = "netkeep" }]\n'
        "entrypoints = [\n"
        f'    {{ name = "netkeep", install-path = "{shim}", from = "netkeep" }},\n'
        "]\n"
    )
    yield environment, python, shim


def test_a_uv_tool_install_points_at_uvs_shim(tool_env: tuple[Path, Path, Path]) -> None:
    environment, python, shim = tool_env
    assert login_item.installed_executable(prefix=environment, python=python) == shim


def test_without_the_shim_it_falls_back_to_the_environments_own(
    tool_env: tuple[Path, Path, Path],
) -> None:
    environment, python, shim = tool_env
    shim.unlink()
    assert login_item.installed_executable(prefix=environment, python=python) == (
        environment / "bin" / "netkeep"
    )


def test_a_shim_replaced_by_another_program_is_not_used(tool_env: tuple[Path, Path, Path]) -> None:
    environment, python, shim = tool_env
    shim.unlink()
    shim.write_text("#!/bin/sh\necho not netkeep\n")
    shim.chmod(0o755)
    assert login_item.installed_executable(prefix=environment, python=python) == (
        environment / "bin" / "netkeep"
    )


def test_a_checkout_without_a_receipt_uses_its_venv(tool_env: tuple[Path, Path, Path]) -> None:
    environment, python, _shim = tool_env
    (environment / login_item.UV_RECEIPT).unlink()
    assert login_item.installed_executable(prefix=environment, python=python) == (
        environment / "bin" / "netkeep"
    )


def test_a_receipt_that_doesnt_parse_is_ignored(tool_env: tuple[Path, Path, Path]) -> None:
    environment, python, _shim = tool_env
    (environment / login_item.UV_RECEIPT).write_text("[tool\n")
    assert login_item.tool_shim(environment, python.parent / "netkeep") is None


# --- the health check, against a fake endpoint on a free loopback port -----------------------


class _Health(BaseHTTPRequestHandler):
    reply: tuple[int, object] = (200, {"status": "ok", "version": "0", "boot_id": "b"})
    paths: list[str] = []

    def do_GET(self) -> None:
        type(self).paths.append(self.path)
        status, body = type(self).reply
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture
def health() -> Iterator[tuple[str, type[_Health]]]:
    handler = type("Health", (_Health,), {"paths": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)  # a free port, never 8000
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", handler
    finally:
        server.shutdown()
        server.server_close()


def test_serve_answers_asks_the_health_endpoint(health: tuple[str, type[_Health]]) -> None:
    base, handler = health
    assert post_install.serve_answers(base)
    assert handler.paths == ["/api/v1/health"]


@pytest.mark.parametrize("reply", [(200, {"status": "starting"}), (503, {"status": "ok"})])
def test_serve_answers_only_for_an_ok_health(
    health: tuple[str, type[_Health]], reply: tuple[int, object]
) -> None:
    base, handler = health
    handler.reply = reply
    assert not post_install.serve_answers(base)


def test_a_closed_port_doesnt_answer() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert not post_install.serve_answers(f"http://127.0.0.1:{port}", timeout_s=0.5)


@pytest.mark.parametrize(
    "base", ["http://192.0.2.10:8123", "https://127.0.0.1:8123", "http://127.0.0.1:99999"]
)
def test_only_a_loopback_http_origin_is_ever_asked(
    base: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_connect(*_args: object, **_kwargs: object) -> socket.socket:
        raise AssertionError("serve_answers connected off loopback")

    monkeypatch.setattr(socket, "create_connection", no_connect)
    assert not post_install.serve_answers(base)


def test_a_test_here_writes_nothing_outside_tmp_path(machine: Machine, tmp_path: Path) -> None:
    """The fakes keep every write under ``tmp_path``: the plist, the logs, the data."""
    assert machine.setup()[0] == 0
    for written in (machine.plist, machine.data / "logs" / "serve.log"):
        assert tmp_path in written.resolve().parents
    assert os.environ[paths.DATA_ENV].startswith(str(tmp_path))
