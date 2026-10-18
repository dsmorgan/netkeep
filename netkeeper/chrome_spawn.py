"""The netkeeper Chrome launcher: the one process netkeeper may start (#636, ADR 0012).

ADR 0012, rule 2: one launcher is the only spawn. It starts the installed Chrome on
netkeeper's dedicated profile and the configured port, and opens the UI there as an
``--app`` window. :func:`spawn_chrome` is the one function in the package that starts
a process, and ``tests/test_browser_safety.py`` pins it: one ``subprocess.Popen`` of
the list :func:`chrome_argv` builds, with no shell. Nothing else here, or anywhere
else, may start one.

**The arguments.** Chrome's binary (``/Applications/Google Chrome.app/Contents/MacOS/
Google Chrome`` on macOS; ``google-chrome`` on Linux, found on ``PATH``), then exactly
the flags ``netkeeper browser launch`` prints
(:func:`netkeeper.services.browser_launch.chrome_flags`: ``--remote-debugging-port=<port>``
and ``--user-data-dir=<profile>``), then ``--app=<the UI's connect link>``, then, on the
first-run path only, LinkedIn's sign-in page as a positional argument. Never a headless
flag, a fresh profile, or any other flag or URL. The ``--app`` link must be exactly
``http://127.0.0.1:<serve's port>/...`` or ``http://localhost:<serve's port>/...``
(:func:`check_app_url`). The binary runs directly, not through ``open -na``, so the
launcher sees that process's own exit status, with no LaunchServices step in between.

**Before a spawn,** :meth:`ChromeLauncher.start` looks, and refuses (saying which case
applied, as ``scripts/chrome.sh --status`` does) when:

- ``linkedin.cdp_url`` names another host, or no valid port: the launcher never falls
  back to Chrome's default port;
- the port answers, but the Chrome on it isn't the dedicated profile
  (:func:`netkeeper.chrome_profile.check_dedicated_profile`, #624, on the pid the
  Chrome on the port names over the attach's connector, under the browser lock). While
  a run holds that lock, no second CDP client may attach (spec 9.9), so the launcher
  uses the pid that run's own attach verified, from the lock's holder note: it must be
  the pid in the profile's ``SingletonLock``, and alive;
- the port is silent, but a live process holds the profile's ``SingletonLock``: a
  hand-off would open the UI in a Chrome that runs can't attach to;
- the port is silent and a run holds a browser lock: a cold start then is refused.
  Opening the UI's window into a running netkeeper Chrome stays allowed during a run.

**Hand-off.** When netkeeper's Chrome is already up, the same command hands its
arguments to it through the profile's singleton: the running Chrome opens the UI's
window and the launched process exits 0. Not verified live: that the running process
honors ``--app`` and the positional URL on a hand-off (docs/design/chrome-profiles.md).
Chrome's singleton can replace a running Chrome that doesn't answer in time, so after
the settle the launcher reads the profile's lock again and reports a success only when
it names the process it expected.

**Detached.** Chrome runs in its own session with its standard streams on
``/dev/null``, so it outlives ``serve`` and a terminal's Ctrl-C.

**The connect code is in Chrome's arguments.** Any local user can read a process's
arguments (``ps -axww``), and a Chrome started cold keeps them for its whole life. The
code there is good once, within 120 seconds (ADR 0013), and Chrome spends it within
about a second.

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
import re
import socket
import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol
from urllib.parse import urlsplit, urlunsplit

from netkeeper.chrome_profile import check_dedicated_profile, profile_lock_pid
from netkeeper.logging_setup import personal_exc
from netkeeper.services.browser_launch import chrome_flags, remote_host_note
from netkeeper.services.chrome_launch import LaunchResult, LaunchStatus, PortProbe, Refusal

log = logging.getLogger(__name__)

#: Chrome's binary on macOS, run directly (not through ``open -na``).
MACOS_CHROME: Final = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
#: Chrome's binary on Linux, found on ``PATH``.
LINUX_CHROME: Final = "google-chrome"
#: The one site URL the launcher may pass, and only from the first-run path.
LINKEDIN_SIGNIN_URL: Final = "https://www.linkedin.com/login"

#: The only netloc an ``--app`` link may have: a loopback name and a port, nothing else
#: (no userinfo, no backslash, no percent-escape, no whitespace).
_APP_NETLOC: Final = re.compile(r"(?:127\.0\.0\.1|localhost):([1-9][0-9]{0,4})")
#: What an ``--app`` link's path, query and fragment may hold: printable ASCII, no
#: backslash and no space, so Chrome's URL parser and Python's read it the same way.
_APP_REST: Final = re.compile(r"[!#-\[\]-~]*")

#: How long a launched process has to exit, as a hand-off does, before it counts as a
#: Chrome that started.
SETTLE_S: Final = 5.0
_POLL_S: Final = 0.1
#: Chrome's ``RESULT_CODE_PROFILE_IN_USE``: a second launch that refused the profile.
PROFILE_IN_USE_EXIT: Final = 21
#: How long the port check waits for a connection on loopback.
_PORT_TIMEOUT_S: Final = 1.0


class LaunchArgumentError(ValueError):
    """The launch's own inputs are wrong: a UI link that isn't netkeeper's loopback UI."""


@dataclass(frozen=True, slots=True)
class ChromeLaunch:
    """Everything the one argument list is built from, all of it config.

    ``app_url`` is the UI's connect link (:func:`netkeeper.services.local_api_key.connect_url`),
    and ``ui_port`` the port ``serve`` listens on, which that link must name.
    ``first_run`` adds LinkedIn's sign-in page; only setup's first-run button sets it.
    """

    port: int
    profile: Path
    app_url: str
    ui_port: int
    first_run: bool = False
    platform: str = sys.platform


def chrome_binary(platform: str) -> str:
    """Chrome's binary on ``platform`` (``sys.platform``)."""
    return MACOS_CHROME if platform == "darwin" else LINUX_CHROME


def check_app_url(url: str, ui_port: int) -> None:
    """Raise :class:`LaunchArgumentError` unless ``url`` is netkeeper's own UI, exactly.

    ``http``, a netloc that is exactly ``127.0.0.1:<ui_port>`` or ``localhost:<ui_port>``,
    a path from ``/``, and nothing two URL parsers could read differently: no userinfo,
    no backslash (Chrome reads it as ``/``), no whitespace, no non-ASCII, and the URL
    rebuilt from its parts must be the same string. So no other site rides in on
    ``--app``.
    """
    parts = urlsplit(url)
    netloc = _APP_NETLOC.fullmatch(parts.netloc)
    if (
        parts.scheme != "http"
        or netloc is None
        or not parts.path.startswith("/")
        or not _APP_REST.fullmatch(url[len(f"http://{parts.netloc}") :])
        or urlunsplit(parts) != url
    ):
        raise LaunchArgumentError("the UI's link must be http://127.0.0.1:<port>/ or localhost")
    port = int(netloc.group(1))
    if not 0 < port < 65536 or port != ui_port:
        raise LaunchArgumentError(f"the UI's link must name serve's port, {ui_port}")


def chrome_argv(launch: ChromeLaunch) -> list[str]:
    """The one argument list: the binary, the printed flags, ``--app``, and the sign-in URL.

    Raises :class:`LaunchArgumentError` when ``app_url`` isn't netkeeper's own UI
    (:func:`check_app_url`) or the debugging port isn't a port.
    """
    check_app_url(launch.app_url, launch.ui_port)
    if not 0 < launch.port < 65536:
        raise LaunchArgumentError(f"{launch.port} is not a debugging port")
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


def configured_port(cdp_url: str) -> int | None:
    """The port ``cdp_url`` names, or ``None`` when it names none or an invalid one.

    Unlike :func:`netkeeper.services.browser_launch.cdp_port`, never Chrome's default:
    the launcher starts Chrome only on a port you configured.
    """
    try:
        port = urlsplit(cdp_url).port
    except ValueError:
        return None
    return port if port is not None and 0 < port < 65536 else None


Spawn = Callable[[ChromeLaunch], Process]


class ChromeLauncher:
    """The checks, then the spawn: a :class:`~netkeeper.services.chrome_launch.ChromeSpawner`.

    Every input is the caller's: ``cdp_url`` (the address in force, read at each launch),
    ``profile`` (the dedicated profile), ``app_url`` (a fresh connect link per launch) and
    ``ui_port`` (serve's port, which that link must name), ``probe`` (what the Chrome on
    the port names for itself, read through the attach's connector under the browser
    lock, or the pid a run holding it verified: :func:`netkeeper.worker.launcher_probe`),
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
        ui_port: int,
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
        self.ui_port = ui_port
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
        port = configured_port(cdp_url)
        if port is None:
            return _refused(
                Refusal.INVALID_PORT,
                f"linkedin.cdp_url ({cdp_url}) names no valid port, and netkeeper starts"
                " Chrome only on the port you set. Fix it in config.toml or on the Settings page",
            )
        answers = port_answers if self._answers is None else self._answers
        expected: int | None
        if await asyncio.to_thread(answers, port):
            verified = await self._dedicated_pid(cdp_url, port)
            if isinstance(verified, LaunchResult):
                return verified
            expected = verified
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
            expected = None
        try:
            launch = ChromeLaunch(
                port=port,
                profile=self.profile,
                app_url=self._app_url(),
                ui_port=self.ui_port,
                first_run=first_run,
                platform=self._platform,
            )
            spawn = spawn_chrome if self._spawn is None else self._spawn
            process = spawn(launch)
        except (OSError, LaunchArgumentError) as exc:
            log.warning("could not start Chrome: %s", personal_exc(exc))
            return LaunchResult(LaunchStatus.FAILED, f"couldn't start Chrome: {exc}")
        return await self._settle(process, expected=expected, port=port)

    async def _dedicated_pid(self, cdp_url: str, port: int) -> int | LaunchResult:
        """The pid of netkeeper's Chrome on ``port``, or the refusal when it isn't shown to be.

        #624's check, on the pid the Chrome on the port names for itself. While a run
        holds the browser, no second CDP client may attach (spec 9.9), so the probe
        connects to nothing and hands back the pid that run's attach verified: it must
        be the pid in the profile's ``SingletonLock``, and alive. Anything else refuses.
        """
        probe = await self._probe(cdp_url)
        lock_pid = profile_lock_pid(self.profile)
        if not probe.busy:
            check = check_dedicated_profile(self.profile, probe.browser_pid)
            if check.dedicated and check.browser_pid is not None:
                return check.browser_pid
            reason = check.reason
        elif (
            probe.verified_pid is not None
            and probe.verified_pid == lock_pid
            and self._pid_alive(lock_pid)
        ):
            return lock_pid
        else:
            reason = (
                "a run holds the browser, and the Chrome its attach verified"
                f" (pid {probe.verified_pid or 'unknown'}) isn't the live one holding"
                f" netkeeper's profile (pid {lock_pid or 'none'}), so the Chrome on that port"
                " can't be shown to be netkeeper's"
            )
        return _refused(
            Refusal.NOT_DEDICATED,
            f"port {port} answers, but {reason}. netkeeper opens its UI only in its own"
            f" Chrome ({self.profile}): quit the Chrome on port {port}, or set"
            " linkedin.cdp_url to a free port",
        )

    async def _settle(self, process: Process, *, expected: int | None, port: int) -> LaunchResult:
        """How the launch went, from the process's exit and the profile's lock afterward.

        A hand-off (``expected`` is the verified Chrome's pid) succeeds only when the
        launched process exits 0 and that pid still holds the profile: Chrome's singleton
        can replace a running Chrome that doesn't answer in time. One that exits nonzero
        with that pid still holding the profile is ``handoff_unconfirmed``: Chrome may
        exit nonzero after notifying the running instance. A pid that changed, or a
        launched process still running, is ``failed``. A cold start
        (``expected`` is ``None``) succeeds only while the launched process runs and no
        other process holds the profile.
        """
        waited = 0.0
        while (status := process.poll()) is None and waited < self._settle_s:
            await self._sleep(_POLL_S)
            waited += _POLL_S
        holder = profile_lock_pid(self.profile)
        if expected is not None:
            still_held = holder == expected and self._pid_alive(expected)
            if status == 0 and still_held:
                log.info("handed the UI's window to netkeeper's Chrome on port %d", port)
                return LaunchResult(
                    LaunchStatus.HANDED_OFF,
                    f"opened the UI in netkeeper's Chrome on port {port}",
                    pid=expected,
                )
            if status is not None and still_held:
                # Chrome may exit nonzero after notifying the running instance; what a
                # headed hand-off exits with is not verified live (the design doc).
                refused = (
                    " (status 21 can mean Chrome refused the hand-off: profile in use)"
                    if status == PROFILE_IN_USE_EXIT
                    else ""
                )
                log.warning("hand-off unconfirmed: the launched Chrome exited %d", status)
                return LaunchResult(
                    LaunchStatus.HANDOFF_UNCONFIRMED,
                    f"handed the UI's window to netkeeper's Chrome (pid {expected}) on port"
                    f" {port}, but the launched process exited with status {status}{refused},"
                    " so netkeeper can't confirm the window opened. It probably opened in the"
                    " running Chrome: check there",
                    pid=expected,
                )
            return _failed(
                f"netkeeper's Chrome (pid {expected}) didn't confirm the UI's window: the"
                f" launched process {'is still running' if status is None else 'exited'}, and"
                f" netkeeper's profile is held by pid {holder or 'none'}. Check netkeeper's"
                " Chrome, then run netkeeper open again"
            )
        if status is not None and status != 0:
            log.warning("Chrome exited with status %d at launch", status)
            return LaunchResult(
                LaunchStatus.FAILED, f"Chrome exited with status {status} at launch"
            )
        if status is None and holder in (None, process.pid):
            log.info("started netkeeper's Chrome (pid %d) on port %d", process.pid, port)
            return LaunchResult(
                LaunchStatus.STARTED,
                f"started netkeeper's Chrome on port {port}, with the UI in its own window",
                pid=process.pid,
            )
        return _failed(
            f"the Chrome netkeeper started (pid {process.pid}) "
            f"{'handed off and exited' if status == 0 else 'is running'}, but netkeeper's"
            f" profile is held by pid {holder or 'none'}, which the checks didn't see."
            " Check netkeeper's Chrome, then run netkeeper open again"
        )


def _refused(refusal: Refusal, message: str) -> LaunchResult:
    log.warning("refused to start Chrome (%s): %s", refusal.value, message)
    return LaunchResult(LaunchStatus.REFUSED, message, refusal=refusal)


def _failed(message: str) -> LaunchResult:
    log.warning("Chrome launch unconfirmed: %s", message)
    return LaunchResult(LaunchStatus.FAILED, message)
