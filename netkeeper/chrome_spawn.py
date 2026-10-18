"""The netkeeper Chrome launcher: the one process netkeeper may start (#636, ADR 0012).

ADR 0012, rule 2: one launcher is the only spawn. It starts the installed Chrome on
netkeeper's dedicated profile and the configured port, and opens the UI there as an
``--app`` window. :func:`spawn_chrome` is the one function in the package that starts
a process, and ``tests/test_browser_safety.py`` pins it: one ``subprocess.Popen`` of
the list :func:`chrome_argv` builds, with no shell. Nothing else here, or anywhere
else, may start one.

**The arguments.** Chrome's binary (``/Applications/Google Chrome.app/Contents/MacOS/
Google Chrome`` on macOS, ``google-chrome`` on Linux), then exactly the flags
``netkeeper browser launch`` prints (:func:`netkeeper.services.browser_launch.chrome_flags`:
``--remote-debugging-port=<port>`` and ``--user-data-dir=<profile>``), then
``--app=<the UI's connect link>``, then, on the first-run path only, LinkedIn's sign-in
page as a positional argument. Never a headless flag, a fresh profile, or any other
flag or URL. The binary runs directly, not through ``open -na``, so the launcher sees
that process's own exit status, with no LaunchServices step in between.

**Before a spawn,** :meth:`ChromeLauncher.start` looks, and refuses (saying which case
applied, as ``scripts/chrome.sh --status`` does) when:

- the port answers, but the Chrome on it isn't the dedicated profile
  (:func:`netkeeper.chrome_profile.check_dedicated_profile`, #624, on the pid the
  Chrome on the port names over the attach's connector, under the browser lock). While
  a run holds that lock, no second CDP client may attach (spec 9.9), so the profile
  must instead name the port in its ``DevToolsActivePort`` file, with the process
  holding its ``SingletonLock`` alive;
- the port is silent, but a live process holds the profile's ``SingletonLock``: a
  hand-off would open the UI in a Chrome that runs can't attach to;
- the port is silent and a run holds a browser lock: a cold start then is refused.
  Opening the UI's window into a running netkeeper Chrome stays allowed during a run;
- ``linkedin.cdp_url`` names another host, which no local Chrome can serve.

**Hand-off.** When netkeeper's Chrome is already up, the same command hands its
arguments to it through the profile's singleton: the running Chrome opens the UI's
window and the launched process exits 0. Not verified live: that the running process
honors ``--app`` and the positional URL on a hand-off (docs/design/chrome-profiles.md).

**Detached.** Chrome runs in its own session with its standard streams on
``/dev/null``, so it outlives ``serve`` and a terminal's Ctrl-C.

**Who may call it.** Only ``netkeeper/cli.py`` (``netkeeper open``) and
``netkeeper/worker.py`` (``serve``'s wiring) import this module; a test pins that
list. The app reaches the launcher only as a
:class:`netkeeper.services.chrome_launch.ChromeSpawner` on ``app.state``, and a route
submits the launch to the task runner. A run never calls it (ADR 0012, rule 5). This
module imports nothing from ``netkeeper.linkedin``: the port's browser pid, the
browser locks and a pid's liveness come in from the caller.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol
from urllib.parse import urlsplit

from netkeeper.chrome_profile import check_dedicated_profile, profile_lock_pid
from netkeeper.logging_setup import personal_exc
from netkeeper.services.browser_launch import cdp_port, chrome_flags, remote_host_note
from netkeeper.services.chrome_launch import LaunchResult, LaunchStatus, PortProbe, Refusal

log = logging.getLogger(__name__)

#: Chrome's binary on macOS, run directly (not through ``open -na``).
MACOS_CHROME: Final = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
#: Chrome's binary on Linux, found on ``PATH``.
LINUX_CHROME: Final = "google-chrome"
#: The one site URL the launcher may pass, and only from the first-run path.
LINKEDIN_SIGNIN_URL: Final = "https://www.linkedin.com/login"

#: The file Chrome writes into its profile with the debugging port it listens on.
DEVTOOLS_ACTIVE_PORT: Final = "DevToolsActivePort"

_LOOPBACK: Final = frozenset({"127.0.0.1", "localhost", "::1"})

#: How long a launched process has to exit, as a hand-off does, before it counts as a
#: Chrome that started.
SETTLE_S: Final = 5.0
_POLL_S: Final = 0.1
#: How long the port check waits for a connection on loopback.
_PORT_TIMEOUT_S: Final = 1.0


class LaunchArgumentError(ValueError):
    """The launch's own inputs are wrong: a UI link that isn't netkeeper's loopback UI."""


@dataclass(frozen=True, slots=True)
class ChromeLaunch:
    """Everything the one argument list is built from, all of it config.

    ``app_url`` is the UI's connect link (:func:`netkeeper.services.local_api_key.connect_url`).
    ``first_run`` adds LinkedIn's sign-in page; only setup's first-run button sets it.
    """

    port: int
    profile: Path
    app_url: str
    first_run: bool = False
    platform: str = sys.platform


def chrome_binary(platform: str) -> str:
    """Chrome's binary on ``platform`` (``sys.platform``)."""
    return MACOS_CHROME if platform == "darwin" else LINUX_CHROME


def chrome_argv(launch: ChromeLaunch) -> list[str]:
    """The one argument list: the binary, the printed flags, ``--app``, and the sign-in URL.

    Raises :class:`LaunchArgumentError` when ``app_url`` isn't an ``http`` link to a
    loopback address, so no other site can ride in on ``--app``.
    """
    parts = urlsplit(launch.app_url)
    if parts.scheme != "http" or parts.hostname not in _LOOPBACK:
        raise LaunchArgumentError("the UI's link must be an http link to this machine")
    argv = [chrome_binary(launch.platform), *chrome_flags(launch.port, launch.profile)]
    argv.append(f"--app={launch.app_url}")
    if launch.first_run:
        argv.append(LINKEDIN_SIGNIN_URL)
    return argv


class Process(Protocol):
    """What the launcher reads of a started process: its pid and, once it exits, its status."""

    @property
    def pid(self) -> int: ...

    def poll(self) -> int | None: ...


def spawn_chrome(launch: ChromeLaunch) -> Process:
    """Start Chrome with :func:`chrome_argv`'s list, detached. netkeeper's only spawn.

    No shell, no other program: ``argv[0]`` is Chrome's binary. Its own session and
    ``/dev/null`` streams keep it running after ``serve`` stops. Raises ``OSError``
    when the binary can't be run.
    """
    argv = chrome_argv(launch)
    return subprocess.Popen(  # noqa: S603 -- Chrome's fixed binary and our own arguments
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def port_answers(port: int) -> bool:
    """Whether anything accepts a connection on loopback ``port``. Sends nothing."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=_PORT_TIMEOUT_S):
            return True
    except OSError:
        return False


def devtools_port(profile: Path) -> int | None:
    """The port in ``profile``'s ``DevToolsActivePort``, or ``None`` when there's none to read.

    Chrome writes the file into its ``--user-data-dir`` when its debugging port opens:
    the port on the first line, the browser's endpoint on the second.
    """
    try:
        first = (profile / DEVTOOLS_ACTIVE_PORT).read_text(encoding="utf-8").split("\n", 1)[0]
    except (OSError, UnicodeDecodeError):
        return None
    first = first.strip()
    return int(first) if first.isdigit() and 0 < int(first) < 65536 else None


Spawn = Callable[[ChromeLaunch], Process]


class ChromeLauncher:
    """The checks, then the spawn: a :class:`~netkeeper.services.chrome_launch.ChromeSpawner`.

    Every input is the caller's: ``cdp_url`` (the address in force, read at each launch),
    ``profile`` (the dedicated profile), ``app_url`` (a fresh connect link per launch),
    ``probe`` (what the Chrome on the port names for itself, read through the attach's
    connector under the browser lock: :func:`netkeeper.worker.launcher_probe`),
    ``run_holds_browser`` (whether a run holds a browser lock:
    :func:`netkeeper.services.runs.any_browser_lock_held`) and ``pid_alive``.
    ``spawn`` and ``answers`` default to :func:`spawn_chrome` and :func:`port_answers`,
    looked up at each launch; tests replace them.
    """

    def __init__(
        self,
        *,
        cdp_url: Callable[[], Awaitable[str]],
        profile: Path,
        app_url: Callable[[], str],
        probe: Callable[[str], Awaitable[PortProbe]],
        run_holds_browser: Callable[[], bool],
        pid_alive: Callable[[int], bool],
        spawn: Spawn | None = None,
        answers: Callable[[int], bool] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        settle_s: float = SETTLE_S,
        platform: str = sys.platform,
    ) -> None:
        self._cdp_url = cdp_url
        self.profile = profile
        self._app_url = app_url
        self._probe = probe
        self._run_holds_browser = run_holds_browser
        self._pid_alive = pid_alive
        self._spawn = spawn
        self._answers = answers
        self._sleep = sleep
        self._settle_s = settle_s
        self._platform = platform

    async def start(self, *, first_run: bool = False) -> LaunchResult:
        """Look, then start netkeeper's Chrome or hand the UI's window to it, or refuse."""
        cdp_url = await self._cdp_url()
        note = remote_host_note(cdp_url)
        if note is not None:
            return _refused(Refusal.REMOTE_ADDRESS, f"{note} netkeeper starts Chrome only here")
        port = cdp_port(cdp_url)
        answers = port_answers if self._answers is None else self._answers
        if await asyncio.to_thread(answers, port):
            refusal = await self._not_dedicated(cdp_url, port)
            if refusal is not None:
                return refusal
            hand_off = True
        else:
            lock_pid = profile_lock_pid(self.profile)
            if lock_pid is not None and self._pid_alive(lock_pid):
                return _refused(
                    Refusal.PROFILE_WITHOUT_PORT,
                    f"a Chrome (pid {lock_pid}) has netkeeper's profile ({self.profile}) open,"
                    f" but port {port} doesn't answer, so runs couldn't attach to it. Quit that"
                    f" Chrome and try again. If pid {lock_pid} isn't Chrome, remove"
                    f" {self.profile / 'SingletonLock'}",
                )
            if await asyncio.to_thread(self._run_holds_browser):
                return _refused(
                    Refusal.RUN_HOLDS_BROWSER,
                    "a run holds the browser, so netkeeper won't start Chrome now. Try again"
                    " when the run ends",
                )
            hand_off = False
        try:
            launch = ChromeLaunch(
                port=port,
                profile=self.profile,
                app_url=self._app_url(),
                first_run=first_run,
                platform=self._platform,
            )
            spawn = spawn_chrome if self._spawn is None else self._spawn
            process = spawn(launch)
        except (OSError, LaunchArgumentError) as exc:
            log.warning("could not start Chrome: %s", personal_exc(exc))
            return LaunchResult(LaunchStatus.FAILED, f"couldn't start Chrome: {exc}")
        return await self._settle(process, hand_off=hand_off, port=port)

    async def _not_dedicated(self, cdp_url: str, port: int) -> LaunchResult | None:
        """The refusal when the Chrome on ``port`` isn't netkeeper's profile, else ``None``.

        #624's check, on the pid the Chrome on the port names for itself. While a run
        holds the browser, no second CDP client may attach (spec 9.9), so the probe
        reads nothing over CDP: then the profile itself must name ``port``, in the
        ``DevToolsActivePort`` file Chrome writes into it, and the process holding the
        profile's lock must be alive. Anything else refuses.
        """
        probe = await self._probe(cdp_url)
        if not probe.busy:
            check = check_dedicated_profile(self.profile, probe.browser_pid)
            if check.dedicated:
                return None
            reason = check.reason
        else:
            lock_pid = profile_lock_pid(self.profile)
            if (
                lock_pid is not None
                and self._pid_alive(lock_pid)
                and devtools_port(self.profile) == port
            ):
                return None
            reason = (
                "a run holds the browser, and netkeeper's profile doesn't show its Chrome"
                f" listening on port {port}, so the Chrome on it can't be shown to be netkeeper's"
            )
        return _refused(
            Refusal.NOT_DEDICATED,
            f"port {port} answers, but {reason}. netkeeper opens its UI only in its own"
            f" Chrome ({self.profile}): quit the Chrome on port {port}, or set"
            " linkedin.cdp_url to a free port",
        )

    async def _settle(self, process: Process, *, hand_off: bool, port: int) -> LaunchResult:
        """How the launched process went: exited 0 (a hand-off), failed, or still running."""
        waited = 0.0
        while (status := process.poll()) is None and waited < self._settle_s:
            await self._sleep(_POLL_S)
            waited += _POLL_S
        if status is None:
            log.info("started netkeeper's Chrome (pid %d) on port %d", process.pid, port)
            return LaunchResult(
                LaunchStatus.STARTED,
                f"started netkeeper's Chrome on port {port}, with the UI in its own window",
                pid=process.pid,
            )
        if status == 0:
            log.info("handed the UI's window to netkeeper's Chrome on port %d", port)
            words = "netkeeper's Chrome" if hand_off else "a netkeeper Chrome that just started"
            return LaunchResult(LaunchStatus.HANDED_OFF, f"opened the UI in {words} on port {port}")
        log.warning("Chrome exited with status %d at launch", status)
        return LaunchResult(LaunchStatus.FAILED, f"Chrome exited with status {status} at launch")


def _refused(refusal: Refusal, message: str) -> LaunchResult:
    log.warning("refused to start Chrome (%s): %s", refusal.value, message)
    return LaunchResult(LaunchStatus.REFUSED, message, refusal=refusal)
