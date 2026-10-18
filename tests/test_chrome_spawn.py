"""``netkeeper/chrome_spawn.py``: the Chrome launcher, its arguments and its refusals (#636).

Fakes only: no test starts Chrome, opens a port or connects to one (``tests/conftest.py``
blocks the real ``spawn_chrome`` and ``port_answers``). The arguments are compared as
parsed arguments with the lines ``netkeeper browser launch`` prints, not as shell text.
Each refusal is shown refusing, and then shown to be what stops the spawn: the same case
against a copy of the module with that one check removed starts Chrome.
"""

from __future__ import annotations

import ast
import shlex
import sys
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from netkeeper import chrome_spawn
from netkeeper.chrome_spawn import (
    LINKEDIN_SIGNIN_URL,
    ChromeLaunch,
    ChromeLauncher,
    LaunchArgumentError,
    check_app_url,
    chrome_argv,
    configured_port,
)
from netkeeper.chrome_spawn import spawn_chrome as real_spawn_chrome
from netkeeper.services.browser_launch import chrome_launch_command
from netkeeper.services.chrome_launch import LaunchStatus, PortProbe, Refusal

UI_PORT = 8123
APP_URL = f"http://127.0.0.1:{UI_PORT}/api/v1/session/connect?code=1.ab.cd"
CDP_URL = "http://127.0.0.1:9333"  # never 9222: tests never name the maintainer's port
PORT = 9333
LOCK_PID = 4242


def test_the_safety_constants_are_these_literals() -> None:
    assert (
        chrome_spawn.MACOS_CHROME == "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    )
    assert chrome_spawn.LINUX_CHROME == "google-chrome"
    assert LINKEDIN_SIGNIN_URL == "https://www.linkedin.com/login"


# --- the one argument list ----------------------------------------------------------------


def _printed_flags(monkeypatch: pytest.MonkeyPatch, platform: str, profile: Path) -> list[str]:
    """``netkeeper browser launch``'s printed lines, parsed as a shell would, minus the
    program: ``open -na "Google Chrome" --args`` on macOS, ``google-chrome`` on Linux."""
    monkeypatch.setattr(sys, "platform", platform)
    lines = chrome_launch_command(PORT, profile)
    tokens = shlex.split(" ".join(line.rstrip("\\").strip() for line in lines))
    opener = (
        ["open", "-na", "Google Chrome", "--args"] if platform == "darwin" else ["google-chrome"]
    )
    assert tokens[: len(opener)] == opener
    return tokens[len(opener) :]


@pytest.mark.parametrize(
    ("platform", "binary"),
    [
        ("darwin", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ("linux", "google-chrome"),
    ],
)
def test_the_arguments_are_the_printed_flags_then_the_ui(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform: str, binary: str
) -> None:
    profile = tmp_path / "data dir" / "chrome-profile"  # a space survives as one argument
    flags = _printed_flags(monkeypatch, platform, profile)
    assert flags == [f"--remote-debugging-port={PORT}", f"--user-data-dir={profile}"]
    argv = chrome_argv(ChromeLaunch(PORT, profile, APP_URL, UI_PORT, platform=platform))
    assert argv == [binary, *flags, f"--app={APP_URL}"]


def test_only_the_first_run_adds_linkedins_sign_in_page(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    flags = _printed_flags(monkeypatch, "darwin", tmp_path)
    argv = chrome_argv(
        ChromeLaunch(PORT, tmp_path, APP_URL, UI_PORT, first_run=True, platform="darwin")
    )
    assert argv[1:] == [*flags, f"--app={APP_URL}", "https://www.linkedin.com/login"]
    assert LINKEDIN_SIGNIN_URL not in chrome_argv(ChromeLaunch(PORT, tmp_path, APP_URL, UI_PORT))


def test_nothing_else_is_ever_added(tmp_path: Path) -> None:
    for first_run in (False, True):
        argv = chrome_argv(ChromeLaunch(PORT, tmp_path, APP_URL, UI_PORT, first_run=first_run))
        flags = [a for a in argv[1:] if a.startswith("-")]
        assert [f.split("=", 1)[0] for f in flags] == [
            "--remote-debugging-port",
            "--user-data-dir",
            "--app",
        ]
        assert not any("headless" in a or "incognito" in a or "temp" in a for a in argv)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/feed/",
        "http://example.com:8123/",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "http://127.0.0.1.example.com/",
        # #658 review: what Python and Chrome would read differently, or another port.
        "http://evil.com\\@127.0.0.1:8123/",
        "http://evil.com@127.0.0.1:8123/",
        "http://user:pass@127.0.0.1:8123/",
        "http://127.0.0.1:8123%5C@evil.com/",
        "http://127.0.0.1%5C@evil.com:8123/",
        "http://127.0.0.1:8123\\evil.com/",
        "http://127.0.0.1:8123/a b",
        "http://127.0.0.1/",
        "http://localhost/",
        "http://127.0.0.1:9000/",
        "http://127.0.0.1:0/",
        "http://127.0.0.1:99999/",
        "http://[::1]:8123/",
        "http://127.0.0.1:8123",
        "http://127.0.0.1:8123/\u00e9",
        "HTTP://127.0.0.1:8123/",
    ],
)
def test_the_app_window_opens_only_netkeepers_loopback_ui(tmp_path: Path, url: str) -> None:
    with pytest.raises(LaunchArgumentError):
        chrome_argv(ChromeLaunch(PORT, tmp_path, url, UI_PORT))


@pytest.mark.parametrize(
    "url",
    [APP_URL, "http://localhost:8123/", "http://127.0.0.1:8123/?connect=expired#x"],
)
def test_netkeepers_own_ui_link_passes(url: str) -> None:
    check_app_url(url, UI_PORT)


@pytest.mark.parametrize(
    ("cdp_url", "port"),
    [
        ("http://127.0.0.1:9333", 9333),
        ("http://127.0.0.1", None),
        ("http://127.0.0.1:0", None),
        ("http://127.0.0.1:notaport", None),
        ("http://127.0.0.1:70000", None),
    ],
)
def test_the_launcher_reads_only_a_configured_port(cdp_url: str, port: int | None) -> None:
    assert configured_port(cdp_url) == port


@dataclass
class _FakeSubprocess:
    """Stands in for the ``subprocess`` module inside ``chrome_spawn`` only."""

    DEVNULL: int = -3
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = field(default_factory=list)

    def Popen(self, *args: Any, **kwargs: Any) -> object:
        self.calls.append((args, kwargs))
        return types.SimpleNamespace(pid=777, poll=lambda: None)


def test_spawn_chrome_runs_the_binary_directly_and_detached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _FakeSubprocess()
    monkeypatch.setattr(chrome_spawn, "subprocess", fake)
    launch = ChromeLaunch(PORT, tmp_path, APP_URL, UI_PORT, platform="darwin")
    process = real_spawn_chrome(launch)
    assert process.pid == 777
    [(args, kwargs)] = fake.calls
    assert args == (chrome_argv(launch),)
    assert args[0][0] == "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    assert kwargs == {
        "stdin": fake.DEVNULL,
        "stdout": fake.DEVNULL,
        "stderr": fake.DEVNULL,
        "start_new_session": True,
    }


# --- the checks, then the spawn -----------------------------------------------------------


@dataclass
class _Process:
    pid: int = 5150
    exits: int | None = None

    def poll(self) -> int | None:
        return self.exits


@dataclass
class _Case:
    """One launch's world, all of it fake."""

    cdp_url: str = CDP_URL
    answers: bool = False
    probe: PortProbe = field(default_factory=lambda: PortProbe(busy=False))
    lock_pid: int | None = None
    alive: bool = True
    run_holds: bool = False
    process: _Process = field(default_factory=_Process)
    #: Whether the launched Chrome takes the profile's lock, as a Chrome that starts
    #: does. By default, whenever the launched process keeps running.
    takes_over: bool | None = None
    spawn_error: OSError | None = None
    spawned: list[ChromeLaunch] = field(default_factory=list)
    asked_ports: list[int] = field(default_factory=list)
    probed: list[str] = field(default_factory=list)
    run_lock_asked: int = 0

    def _run_holds(self) -> bool:
        self.run_lock_asked += 1
        return self.run_holds

    def launcher(self, tmp_path: Path, module: Any = chrome_spawn) -> Any:
        profile = tmp_path / "chrome-profile"
        profile.mkdir(parents=True, exist_ok=True)
        if self.lock_pid is not None:
            (profile / "SingletonLock").symlink_to(f"fakehost.local-{self.lock_pid}")

        async def address() -> str:
            return self.cdp_url

        async def probe(url: str) -> PortProbe:
            self.probed.append(url)
            return self.probe

        def answers(port: int) -> bool:
            self.asked_ports.append(port)
            return self.answers

        def spawn(launch: ChromeLaunch) -> _Process:
            if self.spawn_error is not None:
                raise self.spawn_error
            self.spawned.append(launch)
            takes_over = self.process.exits is None if self.takes_over is None else self.takes_over
            if takes_over:
                lock = profile / "SingletonLock"
                lock.unlink(missing_ok=True)
                lock.symlink_to(f"fakehost.local-{self.process.pid}")
                self.lock_pid = self.process.pid
            return self.process

        async def no_sleep(_s: float) -> None:
            return None

        launcher: ChromeLauncher = module.ChromeLauncher(
            cdp_url=address,
            profile=profile,
            app_url=lambda: APP_URL,
            ui_port=UI_PORT,
            probe=probe,
            run_holds_browser=self._run_holds,
            pid_alive=lambda pid: self.alive and pid == self.lock_pid,
            spawn=spawn,
            answers=answers,
            sleep=no_sleep,
            settle_s=0.3,
            platform="darwin",
        )
        return launcher


async def test_a_cold_start_spawns_on_the_configured_port(tmp_path: Path) -> None:
    case = _Case(cdp_url="http://127.0.0.1:9444")
    result = await case.launcher(tmp_path).start()
    assert result.status is LaunchStatus.STARTED and result.pid == 5150
    assert case.asked_ports == [9444]
    [launch] = case.spawned
    assert launch.port == 9444 and launch.app_url == APP_URL and not launch.first_run
    assert launch.profile == tmp_path / "chrome-profile"
    assert "--remote-debugging-port=9444" in chrome_argv(launch)
    assert case.probed == []  # a silent port is never attached to


async def test_the_first_run_is_passed_through(tmp_path: Path) -> None:
    case = _Case()
    await case.launcher(tmp_path).start(first_run=True)
    [launch] = case.spawned
    assert launch.first_run and chrome_argv(launch)[-1] == LINKEDIN_SIGNIN_URL


async def test_a_running_netkeeper_chrome_gets_the_window(tmp_path: Path) -> None:
    case = _Case(
        answers=True,
        probe=PortProbe(busy=False, browser_pid=LOCK_PID),
        lock_pid=LOCK_PID,
        process=_Process(exits=0),
    )
    result = await case.launcher(tmp_path).start()
    assert result.status is LaunchStatus.HANDED_OFF, result
    assert case.probed == [CDP_URL] and len(case.spawned) == 1


async def test_a_hand_off_is_allowed_while_a_run_holds_the_browser(tmp_path: Path) -> None:
    # A run is attached, so the probe opened nothing; the run's attach verified the pid.
    case = _Case(
        answers=True,
        probe=PortProbe(busy=True, verified_pid=LOCK_PID),
        lock_pid=LOCK_PID,
        run_holds=True,
        process=_Process(exits=0),
    )
    result = await case.launcher(tmp_path).start()
    assert result.status is LaunchStatus.HANDED_OFF, result
    assert len(case.spawned) == 1


async def test_a_lock_left_by_a_crash_does_not_refuse(tmp_path: Path) -> None:
    case = _Case(lock_pid=LOCK_PID, alive=False)
    assert (await case.launcher(tmp_path).start()).status is LaunchStatus.STARTED


async def test_a_chrome_that_exits_with_an_error_failed(tmp_path: Path) -> None:
    case = _Case(process=_Process(exits=21))
    result = await case.launcher(tmp_path).start()
    assert result.status is LaunchStatus.FAILED and "status 21" in result.message


async def test_a_chrome_that_cannot_be_run_failed(tmp_path: Path) -> None:
    case = _Case(spawn_error=FileNotFoundError("no such file: google-chrome"))
    result = await case.launcher(tmp_path).start()
    assert result.status is LaunchStatus.FAILED and "no such file" in result.message


# --- each refusal, and the check that makes it --------------------------------------------


@dataclass(frozen=True)
class _Refusal:
    case: Callable[[], _Case]
    refusal: Refusal
    words: str
    #: The check, and the same source with it removed.
    check: str
    without: str


REFUSALS = {
    "another chrome on the port": _Refusal(
        lambda: _Case(
            answers=True, probe=PortProbe(busy=False, browser_pid=999), lock_pid=LOCK_PID
        ),
        Refusal.NOT_DEDICATED,
        "isn't the one holding netkeeper's profile",
        "            if check.dedicated and check.browser_pid is not None:\n",
        "            if True:\n",
    ),
    "a chrome on the port that names no pid": _Refusal(
        lambda: _Case(answers=True, probe=PortProbe(busy=False), lock_pid=LOCK_PID),
        Refusal.NOT_DEDICATED,
        "didn't say which process it is",
        "            if check.dedicated and check.browser_pid is not None:\n",
        "            if True:\n",
    ),
    "during a run, the verified chrome isn't the profile's": _Refusal(
        lambda: _Case(
            answers=True, probe=PortProbe(busy=True, verified_pid=999), lock_pid=LOCK_PID
        ),
        Refusal.NOT_DEDICATED,
        "a run holds the browser",
        "            and probe.verified_pid == lock_pid\n",
        "",
    ),
    "during a run, no verified chrome yet": _Refusal(
        lambda: _Case(answers=True, probe=PortProbe(busy=True), lock_pid=LOCK_PID),
        Refusal.NOT_DEDICATED,
        "a run holds the browser",
        "            probe.verified_pid is not None\n"
        "            and probe.verified_pid == lock_pid\n",
        "            lock_pid is not None\n",
    ),
    "during a run, the verified chrome is gone": _Refusal(
        lambda: _Case(
            answers=True,
            probe=PortProbe(busy=True, verified_pid=LOCK_PID),
            lock_pid=LOCK_PID,
            alive=False,
        ),
        Refusal.NOT_DEDICATED,
        "a run holds the browser",
        "            and self._pid_alive(lock_pid)\n",
        "",
    ),
    "a chrome address with no valid port": _Refusal(
        lambda: _Case(cdp_url="http://127.0.0.1:notaport"),
        Refusal.INVALID_PORT,
        "names no valid port",
        "        if port is None:\n",
        "        if False:\n",
    ),
    "the profile open without the port": _Refusal(
        lambda: _Case(lock_pid=LOCK_PID),
        Refusal.PROFILE_WITHOUT_PORT,
        f"a Chrome (pid {LOCK_PID}) has netkeeper's profile",
        "            if lock_pid is not None and self._pid_alive(lock_pid):\n",
        "            if False:\n",
    ),
    "a cold start while a run holds the browser": _Refusal(
        lambda: _Case(run_holds=True),
        Refusal.RUN_HOLDS_BROWSER,
        "a run holds the browser",
        "            if await asyncio.to_thread(self._run_holds_browser):\n",
        "            if False:\n",
    ),
    "a chrome address on another host": _Refusal(
        lambda: _Case(cdp_url="http://192.0.2.7:9333"),
        Refusal.REMOTE_ADDRESS,
        "points at 192.0.2.7",
        "        if note is not None:\n",
        "        if False:\n",
    ),
}


@pytest.mark.parametrize("name", REFUSALS)
async def test_each_refusal_refuses_says_which_and_starts_nothing(
    tmp_path: Path, name: str
) -> None:
    expected = REFUSALS[name]
    case = expected.case()
    result = await case.launcher(tmp_path).start()
    assert result.status is LaunchStatus.REFUSED, result
    assert result.refusal is expected.refusal
    assert expected.words in result.message
    assert case.spawned == []


def _without(monkeypatch: pytest.MonkeyPatch, old: str, new: str) -> types.ModuleType:
    """A copy of ``chrome_spawn`` with ``old`` replaced; the real module is untouched."""
    source = Path(chrome_spawn.__file__).read_text(encoding="utf-8")
    assert source.count(old) == 1, old
    module = types.ModuleType("netkeeper_chrome_spawn_mutant")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(source.replace(old, new), "<chrome_spawn mutant>", "exec"), module.__dict__)
    return module


@pytest.mark.parametrize("name", REFUSALS)
async def test_without_its_check_each_refused_case_would_start_chrome(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    """The mutation half: the refusal above comes from that check and nothing else."""
    expected = REFUSALS[name]
    case = expected.case()
    case.process = _Process(exits=0)
    mutant = _without(monkeypatch, expected.check, expected.without)
    result = await case.launcher(tmp_path, module=mutant).start()
    assert result.status is not LaunchStatus.REFUSED, result
    assert len(case.spawned) == 1


async def test_a_hand_off_never_asks_about_runs_but_a_cold_start_does(tmp_path: Path) -> None:
    hand_off = _Case(
        answers=True,
        probe=PortProbe(busy=False, browser_pid=LOCK_PID),
        lock_pid=LOCK_PID,
        run_holds=True,
        process=_Process(exits=0),
    )
    assert (await hand_off.launcher(tmp_path / "a").start()).opened
    assert hand_off.run_lock_asked == 0
    cold = _Case()
    assert (await cold.launcher(tmp_path / "b").start()).opened
    assert cold.run_lock_asked == 1


# --- only the first-run path passes LinkedIn's URL ----------------------------------------

PACKAGE = Path(chrome_spawn.__file__).resolve().parent


def _first_run_keywords() -> dict[str, list[str]]:
    """Every ``first_run=`` keyword in the package, by file, as source text."""
    found: dict[str, list[str]] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg == "first_run":
                        rel = str(path.relative_to(PACKAGE.parent))
                        found.setdefault(rel, []).append(ast.unparse(keyword.value))
    return found


def test_only_the_routes_first_run_flag_reaches_the_launcher() -> None:
    """``netkeeper open`` never passes ``first_run``; the route passes the body's flag,
    which only setup's first-run button sets (#599), and nothing passes a literal True."""
    assert _first_run_keywords() == {
        "netkeeper/chrome_spawn.py": ["first_run"],  # launch() into its ChromeLaunch
        "netkeeper/web/api/linkedin.py": ["first_run"],  # the body's flag, into launch()
    }
    users = [
        str(path.relative_to(PACKAGE.parent))
        for path in sorted(PACKAGE.rglob("*.py"))
        if "LINKEDIN_SIGNIN_URL" in path.read_text(encoding="utf-8")
    ]
    assert users == ["netkeeper/chrome_spawn.py"]


# --- the settle: success only when the profile's holder is the one expected ----------------


async def test_a_hand_off_that_replaced_a_hung_chrome_is_not_a_success(tmp_path: Path) -> None:
    """Chrome's singleton can kill a running Chrome that doesn't answer and take the
    profile; the launcher must not report that as a hand-off or a start (#658 review)."""
    case = _Case(
        answers=True,
        probe=PortProbe(busy=False, browser_pid=LOCK_PID),
        lock_pid=LOCK_PID,
        process=_Process(exits=None),
    )
    result = await case.launcher(tmp_path).start()
    assert result.status is LaunchStatus.FAILED, result
    assert f"pid {LOCK_PID}" in result.message and "pid 5150" in result.message


async def test_a_hand_off_whose_process_hangs_is_not_a_success(tmp_path: Path) -> None:
    case = _Case(
        answers=True,
        probe=PortProbe(busy=False, browser_pid=LOCK_PID),
        lock_pid=LOCK_PID,
        process=_Process(exits=None),
        takes_over=False,
    )
    assert (await case.launcher(tmp_path).start()).status is LaunchStatus.FAILED


async def test_a_hand_off_after_which_another_pid_holds_the_profile_failed(
    tmp_path: Path,
) -> None:
    case = _Case(
        answers=True,
        probe=PortProbe(busy=False, browser_pid=LOCK_PID),
        lock_pid=LOCK_PID,
        process=_Process(exits=0),
        takes_over=True,
    )
    assert (await case.launcher(tmp_path).start()).status is LaunchStatus.FAILED


async def test_a_cold_start_another_chrome_took_is_not_a_start(tmp_path: Path) -> None:
    # Exit 0 on a cold start: a Chrome the checks didn't see took the arguments.
    case = _Case(process=_Process(exits=0))
    result = await case.launcher(tmp_path).start()
    assert result.status is LaunchStatus.FAILED, result


@pytest.mark.parametrize("status", [3, 1])
async def test_a_hand_off_that_exits_with_another_code_is_unconfirmed_not_failed(
    tmp_path: Path, status: int
) -> None:
    """Chrome may exit nonzero after notifying the running instance (#658 re-review)."""
    case = _Case(
        answers=True,
        probe=PortProbe(busy=False, browser_pid=LOCK_PID),
        lock_pid=LOCK_PID,
        process=_Process(exits=status),
    )
    result = await case.launcher(tmp_path).start()
    assert result.status is LaunchStatus.HANDOFF_UNCONFIRMED, result
    assert result.reached_chrome and not result.opened
    assert f"exited with status {status}" in result.message
    assert "probably opened in the running Chrome" in result.message


@pytest.mark.parametrize(
    ("status", "words"),
    [(21, "Chrome refused the hand-off: the profile is in use"), (-11, "killed by signal 11")],
)
async def test_a_hand_off_refused_as_profile_in_use_or_killed_failed(
    tmp_path: Path, status: int, words: str
) -> None:
    case = _Case(
        answers=True,
        probe=PortProbe(busy=False, browser_pid=LOCK_PID),
        lock_pid=LOCK_PID,
        process=_Process(exits=status),
    )
    result = await case.launcher(tmp_path).start()
    assert result.status is LaunchStatus.FAILED, result
    assert not result.reached_chrome
    assert words in result.message and "probably opened" not in result.message


async def test_a_nonzero_hand_off_after_which_another_pid_holds_the_profile_failed(
    tmp_path: Path,
) -> None:
    case = _Case(
        answers=True,
        probe=PortProbe(busy=False, browser_pid=LOCK_PID),
        lock_pid=LOCK_PID,
        process=_Process(exits=21),
        takes_over=True,
    )
    assert (await case.launcher(tmp_path).start()).status is LaunchStatus.FAILED
