"""The login item: a per-user launchd agent that runs ``netkeeper serve`` at login (#600).

One implementation behind the Settings switch, ``netkeeper login-item`` and (ADR 0011,
rule 6) ``netkeeper setup``. macOS only. The agent's state is read from the plist file
and from ``launchctl``, never from a stored flag, so a plist someone removed by hand or
a job someone booted out shows as it is.

- **Label.** ``fun.tnkr.netkeeper`` for the default data directory; any other one gets
  ``fun.tnkr.netkeeper.<hash of its resolved path>``, the same hash the Keychain service
  uses (:func:`netkeeper.paths.data_dir_digest`), so two data directories each get their
  own agent and never replace or boot out the other's.
- **Plist.** ``~/Library/LaunchAgents/<label>.plist``, mode 0644, running
  ``<installed netkeeper> serve`` with ``NETKEEPER_DATA`` set to the directory in use.
  stdout and stderr go to ``<data>/logs/serve.log`` and ``serve.err.log``; netkeeper
  writes its own ``netkeeper.log`` there too.
- **The serve lock.** A second ``serve`` on one data directory refuses to start (#590),
  and ``KeepAlive`` would relaunch it every ten seconds for ever. So :meth:`LoginItem.enable`
  never bootstraps the job while a ``serve`` holds the lock: it writes the plist and
  enables it, and launchd loads it at the next login.

Nothing here runs ``launchctl`` or touches ``~/Library/LaunchAgents`` except through the
``runner`` and ``agents_dir`` a :class:`LoginItem` is given; tests give it fakes
(``tests/conftest.py`` makes the defaults raise).
"""

from __future__ import annotations

import logging
import os
import plistlib
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from netkeeper import paths
from netkeeper.logging_setup import LOG_FILENAME
from netkeeper.services import keychain, serve_lock

log = logging.getLogger(__name__)

LAUNCHCTL: Final = "/bin/launchctl"
LABEL_BASE: Final = "fun.tnkr.netkeeper"
SERVE_LOG: Final = "serve.log"
SERVE_ERR_LOG: Final = "serve.err.log"
UNSUPPORTED_REASON: Final = (
    "Run at login uses launchd, which only macOS has. Run netkeeper in its container"
    " instead (docs/architecture.md, section 16)."
)

_LAUNCHCTL_TIMEOUT_S: Final = 15
_STOP_WAIT_S: Final = 5.0
_POLL_S: Final = 0.1
_FILE_MODE: Final = 0o644
_LOG_MODE: Final = 0o600
_DEFERRED_STOP_S: Final = 1.0


class LoginItemError(RuntimeError):
    """The login item could not be installed or removed; the message says why."""


class UnsupportedPlatform(LoginItemError):
    """Not macOS."""


@dataclass(frozen=True, slots=True)
class RunResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[Sequence[str]], RunResult]
"""Runs ``launchctl`` with these arguments. Tests pass a fake."""


def run_launchctl(args: Sequence[str]) -> RunResult:
    """The real runner: ``launchctl <args>``."""
    try:
        done = subprocess.run(  # noqa: S603 -- a fixed binary and our own arguments
            [LAUNCHCTL, *args],
            capture_output=True,
            text=True,
            timeout=_LAUNCHCTL_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LoginItemError(f"launchctl {' '.join(args[:1])} failed: {exc}") from exc
    return RunResult(done.returncode, done.stdout, done.stderr)


def default_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def label_for(data: Path) -> str:
    """The agent's label for ``data``: the plain label for the default data directory."""
    if paths.is_default_data_dir(data):
        return LABEL_BASE
    return f"{LABEL_BASE}.{paths.data_dir_digest(data)}"


def installed_executable() -> Path | None:
    """The ``netkeeper`` that belongs to the running interpreter: the one beside it in
    the environment's ``bin`` (a uv tool, or a checkout's ``.venv``), else the first on
    ``PATH``. Not resolved, so a symlink the installer keeps stable stays the plist's
    target across upgrades."""
    beside = Path(sys.executable).parent / "netkeeper"
    if beside.is_file() and os.access(beside, os.X_OK):
        return beside
    found = shutil.which("netkeeper")
    return Path(found) if found else None


def _is_macos() -> bool:
    platform: str = sys.platform  # read at call time; plain str so mypy checks both branches
    return platform == "darwin"


@dataclass(frozen=True, slots=True)
class Status:
    supported: bool
    reason: str | None
    """Why it is not supported; None when it is."""
    label: str
    plist_path: Path
    log_path: Path
    """``<data>/logs/netkeeper.log``: the application log."""
    installed: bool
    """The plist exists."""
    disabled: bool
    """launchd has the label disabled, so it would not start at login."""
    loaded: bool
    """launchd has the job loaded now (running, or waiting to restart)."""
    this_serve_is_the_agent: bool
    """The ``serve`` answering this call was started by the agent."""
    plist_executable: str | None
    """What the plist runs; differs from the installed ``netkeeper`` after a move."""
    stale: bool

    @property
    def enabled(self) -> bool:
        """Starts at login: the plist is there and launchd will load it."""
        return self.installed and not self.disabled


@dataclass(frozen=True, slots=True)
class Outcome:
    status: Status
    started: bool
    """launchd was told to start the agent now."""
    message: str


class LoginItem:
    """Install, remove and inspect the login item for one data directory."""

    def __init__(
        self,
        *,
        data: Path | None = None,
        agents_dir: Path | None = None,
        runner: Runner | None = None,
        uid: int | None = None,
        executable: Path | None = None,
        macos: bool | None = None,
        environ: Mapping[str, str] | None = None,
        lock_held: Callable[[], bool] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        defer: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self._data = (data if data is not None else paths.data_dir()).expanduser().resolve()
        self._agents_dir = agents_dir if agents_dir is not None else default_agents_dir()
        self._runner = runner if runner is not None else run_launchctl
        self._uid = uid if uid is not None else os.getuid()
        self._executable = executable if executable is not None else installed_executable()
        self._macos = _is_macos() if macos is None else macos
        self._environ = os.environ if environ is None else environ
        self._lock_held = lock_held if lock_held is not None else _serve_lock_held
        self._sleep = sleep
        self._defer = defer if defer is not None else _defer_a_second

    # -- names ------------------------------------------------------------------------

    @property
    def label(self) -> str:
        return label_for(self._data)

    @property
    def plist_path(self) -> Path:
        return self._agents_dir / f"{self.label}.plist"

    @property
    def _domain(self) -> str:
        return f"gui/{self._uid}"

    @property
    def _service(self) -> str:
        return f"{self._domain}/{self.label}"

    @property
    def _logs(self) -> Path:
        return self._data / "logs"

    # -- state ------------------------------------------------------------------------

    def status(self) -> Status:
        log_path = self._logs / LOG_FILENAME
        if not self._macos:
            return Status(
                supported=False,
                reason=UNSUPPORTED_REASON,
                label=self.label,
                plist_path=self.plist_path,
                log_path=log_path,
                installed=False,
                disabled=False,
                loaded=False,
                this_serve_is_the_agent=False,
                plist_executable=None,
                stale=False,
            )
        executable = self._plist_executable()
        installed = self.plist_path.is_file()
        return Status(
            supported=True,
            reason=None,
            label=self.label,
            plist_path=self.plist_path,
            log_path=log_path,
            installed=installed,
            disabled=self._is_disabled(),
            loaded=self._is_loaded(),
            this_serve_is_the_agent=self._environ.get("XPC_SERVICE_NAME") == self.label,
            plist_executable=executable,
            stale=installed
            and self._executable is not None
            and executable != str(self._executable),
        )

    def _plist_executable(self) -> str | None:
        try:
            with self.plist_path.open("rb") as handle:
                arguments = plistlib.load(handle).get("ProgramArguments")
        except (OSError, plistlib.InvalidFileException, ValueError):
            return None
        if isinstance(arguments, list) and arguments and isinstance(arguments[0], str):
            return arguments[0]
        return None

    def _is_loaded(self) -> bool:
        return self._runner(["print", self._service]).returncode == 0

    def _is_disabled(self) -> bool:
        done = self._runner(["print-disabled", self._domain])
        if done.returncode != 0:
            return False
        for line in done.stdout.splitlines():
            name, _, state = line.partition("=>")
            if name.strip().strip('"') == self.label:
                return state.strip().rstrip(",") == "disabled"
        return False

    # -- the plist --------------------------------------------------------------------

    def render_plist(self, *, host: str | None = None, port: int | None = None) -> bytes:
        if self._executable is None:
            raise LoginItemError(
                "Cannot find the installed netkeeper command to run at login. Install"
                " netkeeper (make install) and try again."
            )
        arguments = [str(self._executable), "serve"]
        if host:
            arguments += ["--host", host]
        if port is not None:
            arguments += ["--port", str(port)]
        env = {
            paths.DATA_ENV: str(self._data),
            "NETKEEPER_LOG_STDERR_LEVEL": "WARNING",
        }
        # Settings that name where secrets and config live must follow the agent, or it
        # would read another Keychain service or config file than the serve you set up.
        for name in (keychain.SERVICE_ENV, paths.CONFIG_ENV):
            value = self._environ.get(name, "").strip()
            if value:
                env[name] = value
        document = {
            "Label": self.label,
            "ProgramArguments": arguments,
            "WorkingDirectory": str(self._data),
            "EnvironmentVariables": env,
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 10,
            "StandardOutPath": str(self._logs / SERVE_LOG),
            "StandardErrorPath": str(self._logs / SERVE_ERR_LOG),
        }
        return plistlib.dumps(document, fmt=plistlib.FMT_XML)

    def _write_plist(self, content: bytes) -> None:
        self._agents_dir.mkdir(parents=True, exist_ok=True)
        self._logs.mkdir(parents=True, exist_ok=True)
        self._logs.chmod(0o700)
        for name in (SERVE_LOG, SERVE_ERR_LOG):  # may hold warning text: private, like the log
            os.close(os.open(self._logs / name, os.O_CREAT | os.O_APPEND | os.O_WRONLY, _LOG_MODE))
        target = self.plist_path
        temporary = target.with_name(f".{target.name}.tmp")
        descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, _FILE_MODE)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                os.fchmod(handle.fileno(), _FILE_MODE)  # the umask cannot narrow it
            temporary.replace(target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    # -- turning it on and off --------------------------------------------------------

    def enable(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        serving_here: bool = False,
    ) -> Outcome:
        """Install or replace the agent.

        ``serving_here`` says the caller is a ``serve`` itself (the Settings switch is):
        the lock is then held by definition. A held lock means the agent is registered
        and enabled but not started, so there is never a second ``serve``.
        """
        self._require_macos()
        content = self.render_plist(host=host, port=port)
        previous = self.plist_path.read_bytes() if self.plist_path.is_file() else None
        self._write_plist(content)
        try:
            return self._load(changed=previous != content, serving_here=serving_here)
        except LoginItemError:
            if previous is None:  # leave the state as it was: nothing half-installed
                self.plist_path.unlink(missing_ok=True)
            else:
                self._write_plist(previous)
            raise

    def _load(self, *, changed: bool, serving_here: bool) -> Outcome:
        self._launchctl("enable", self._service)  # clears a `launchctl disable` from before
        loaded = self._is_loaded()
        if loaded and self._environ.get("XPC_SERVICE_NAME") == self.label:
            # This serve is the agent. Booting it out would stop the server answering.
            note = "The login item was already on and is running this server." + (
                " Its new settings apply the next time it starts." if changed else ""
            )
            return Outcome(self.status(), started=False, message=note)
        if loaded:
            self._launchctl("bootout", self._service)
            self._wait_until(lambda: not self._is_loaded())
            self._wait_until(lambda: not self._lock_held())
        if serving_here or self._lock_held():
            return Outcome(
                self.status(),
                started=False,
                message=(
                    "Turned on. netkeeper is running now, so launchd will start it at your next"
                    " login, not before. Starting another copy now would only fail against the"
                    " one running."
                ),
            )
        self._launchctl("bootstrap", self._domain, str(self.plist_path))
        return Outcome(
            self.status(),
            started=True,
            message="Turned on. netkeeper is starting now and at every login.",
        )

    def disable(self, *, serving_here: bool = False) -> Outcome:
        """Stop the agent and remove its plist. Data and logs stay.

        When this ``serve`` is the agent (``serving_here``), stopping it ends the server
        answering, so the stop runs a second after the reply.
        """
        self._require_macos()
        was_loaded = self._is_loaded()
        # The plist goes first: if the stop below fails, the item is still off at login.
        self.plist_path.unlink(missing_ok=True)
        message = "Turned off. netkeeper will not start at login."
        if was_loaded:
            if serving_here and self._environ.get("XPC_SERVICE_NAME") == self.label:
                self._defer(lambda: self._bootout_quietly())
                message = (
                    "Turned off. This copy of netkeeper was started by the login item, so it"
                    " stops in a moment. Run netkeeper serve to start it again."
                )
            else:
                self._launchctl("bootout", self._service)
                self._wait_until(lambda: not self._is_loaded())
        return Outcome(self.status(), started=False, message=message)

    # -- helpers ----------------------------------------------------------------------

    def _bootout_quietly(self) -> None:
        try:
            self._runner(["bootout", self._service])
        except LoginItemError:
            log.warning("could not stop the login item", exc_info=True)

    def _launchctl(self, *args: str) -> None:
        done = self._runner(list(args))
        if done.returncode != 0:
            detail = (done.stderr or done.stdout).strip() or f"exit status {done.returncode}"
            raise LoginItemError(f"launchctl {args[0]} failed: {detail}")

    def _wait_until(self, condition: Callable[[], bool]) -> None:
        """bootout returns before the job is gone, and a bootstrap that races it fails."""
        for _ in range(int(_STOP_WAIT_S / _POLL_S)):
            if condition():
                return
            self._sleep(_POLL_S)

    def _require_macos(self) -> None:
        if not self._macos:
            raise UnsupportedPlatform(UNSUPPORTED_REASON)


def _serve_lock_held() -> bool:
    return serve_lock.state().held


def _defer_a_second(action: Callable[[], None]) -> None:
    timer = threading.Timer(_DEFERRED_STOP_S, action)
    timer.daemon = True
    timer.start()
