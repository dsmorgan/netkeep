"""The login item: a per-user launchd agent that runs ``netkeep serve`` at login (#600).

One implementation behind the Settings switch, ``netkeep login-item`` and (ADR 0011,
rule 6) ``netkeep setup``. macOS only. The agent's state is read from the plist file
and from ``launchctl``, never from a stored flag, so a plist someone removed by hand or
a job someone booted out shows as it is.

- **Label.** ``fun.tnkr.netkeep`` for the default data directory; any other one gets
  ``fun.tnkr.netkeep.<hash of its resolved path>``, the same hash the Keychain service
  uses (:func:`netkeep.paths.data_dir_digest`), so two data directories each get their
  own agent and never replace or boot out the other's. Before the rename (#661) the
  labels began ``fun.tnkr.netkeeper``; such an agent for this data directory is adopted
  like the old script's: :meth:`LoginItem.enable` replaces it, :meth:`LoginItem.disable`
  removes it.
- **Plist.** ``~/Library/LaunchAgents/<label>.plist``, mode 0644, running
  ``<installed netkeep> serve`` with ``NETKEEP_DATA`` set to the directory in use.
  stdout and stderr go to ``<data>/logs/serve.log`` and ``serve.err.log``; netkeep
  writes its own ``netkeep.log`` there too.
- **The serve lock.** A second ``serve`` on one data directory refuses to start (#590),
  and ``KeepAlive`` would relaunch it every ten seconds for ever. So :meth:`LoginItem.enable`
  never bootstraps the job while a ``serve`` holds the lock: it writes the plist and
  enables it, and launchd loads it at the next login.

Nothing here runs ``launchctl`` or touches ``~/Library/LaunchAgents`` except through the
``runner`` and ``agents_dir`` a :class:`LoginItem` is given; tests give it fakes
(``tests/conftest.py`` makes the defaults raise).
"""

from __future__ import annotations

import functools
import logging
import os
import plistlib
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final

from netkeep import paths
from netkeep.logging_setup import LOG_FILENAME
from netkeep.services import keychain, serve_lock

log = logging.getLogger(__name__)

LAUNCHCTL: Final = "/bin/launchctl"
LABEL_BASE: Final = "fun.tnkr.netkeep"
LEGACY_LABEL_BASE: Final = "fun.tnkr.netkeeper"
"""The label before the rename to netkeep (#661), and the old script's."""
LEGACY_DATA_ENV: Final = "NETKEEPER_DATA"
"""What an agent from before the rename names its data directory with."""
SERVE_LOG: Final = "serve.log"
SERVE_ERR_LOG: Final = "serve.err.log"
UNSUPPORTED_REASON: Final = (
    "Run at login uses launchd, which only macOS has. Run netkeep in its container"
    " instead (docs/architecture.md, section 16)."
)

_LAUNCHCTL_TIMEOUT_S: Final = 15
_STOP_WAIT_S: Final = 5.0
_LOCK_WAIT_S: Final = 50.0
"""A serve shuts down within this: the campaign engine can wait 45 s for a send."""
WRAPPER_NAME: Final = "serve-launchd.sh"
"""The script the removed install-launchd.sh pointed its agent at."""
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


def legacy_label_for(data: Path) -> str:
    """What :func:`label_for` was before the rename (#661): the same hash, the old base."""
    if paths.is_default_data_dir(data):
        return LEGACY_LABEL_BASE
    return f"{LEGACY_LABEL_BASE}.{paths.data_dir_digest(data)}"


UV_RECEIPT: Final = "uv-receipt.toml"
"""What ``uv tool install`` writes into a tool's environment: its entry points, with the
path of each one's shim in uv's bin folder."""


def installed_executable(*, prefix: Path | None = None, python: Path | None = None) -> Path | None:
    """The ``netkeep`` that belongs to the running interpreter (``python``, by default
    ``sys.executable``, in the environment at ``prefix``, by default ``sys.prefix``).

    Under ``uv tool install`` that is the shim uv made in its bin folder, such as
    ``~/.local/bin/netkeep`` (:func:`tool_shim`), never the tool environment's own
    ``bin``. Otherwise it is the one beside the interpreter (a checkout's ``.venv``),
    else the first on ``PATH``. Not resolved, so a symlink the installer keeps stable
    stays the plist's target across upgrades."""
    environment = Path(sys.prefix) if prefix is None else prefix
    interpreter = Path(sys.executable) if python is None else python
    beside = interpreter.parent / "netkeep"
    shim = tool_shim(environment, beside)
    if shim is not None:
        return shim
    if _runnable(beside):
        return beside
    found = shutil.which("netkeep")
    return Path(found) if found else None


def tool_shim(environment: Path, beside: Path) -> Path | None:
    """The shim ``uv tool install`` made for netkeep, when ``environment`` is a uv tool's.

    Read from the ``install-path`` of the ``netkeep`` entry point in the environment's
    :data:`UV_RECEIPT`, which follows ``UV_TOOL_BIN_DIR`` and the XDG folders as uv did
    at install time. It counts only when it is an absolute path, is there, and is the
    same file as ``beside`` (uv symlinks it), so a shim since replaced by another program
    is never the target.
    """
    try:
        receipt = tomllib.loads((environment / UV_RECEIPT).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    tool = receipt.get("tool")
    entrypoints = tool.get("entrypoints") if isinstance(tool, dict) else None
    for entry in entrypoints if isinstance(entrypoints, list) else ():
        if not isinstance(entry, dict) or entry.get("name") != "netkeep":
            continue
        install_path = entry.get("install-path")
        if not isinstance(install_path, str) or not install_path:
            continue
        shim = Path(install_path)
        if shim.is_absolute() and _runnable(shim) and _same_file(shim, beside):
            return shim
    return None


def _runnable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _same_file(left: Path, right: Path) -> bool:
    try:
        return left.samefile(right)
    except OSError:
        return False


def _is_macos() -> bool:
    platform: str = sys.platform  # read at call time; plain str so mypy checks both branches
    return platform == "darwin"


class State(StrEnum):
    """The login item's state in the words the Settings card and ``login-item status``
    both show. The value is the text."""

    OFF = "Off"
    REGISTERED = "Registered: starts at next login"
    RUNNING = "Running from login agent"
    NEEDS_REPAIR = "Needs repair"
    DIFFERENT_NETKEEP = "Points at a different netkeep"


@dataclass(frozen=True, slots=True)
class Status:
    supported: bool
    reason: str | None
    """Why it is not supported; None when it is."""
    label: str
    plist_path: Path
    log_path: Path
    """``<data>/logs/netkeep.log``: the application log."""
    installed: bool
    """The plist exists."""
    disabled: bool
    """launchd has the label disabled, so it would not start at login."""
    loaded: bool
    """launchd has the job loaded now (running, or waiting to restart)."""
    this_serve_is_the_agent: bool
    """The ``serve`` answering this call was started by the agent."""
    plist_executable: str | None
    """What the plist runs; differs from the installed ``netkeep`` after a move."""
    stale: bool
    legacy: bool
    """An agent the removed install-launchd.sh wrote serves this data directory. Its
    wrapper script is gone, so it cannot start; enabling adopts it."""

    @property
    def enabled(self) -> bool:
        """Starts at login: the plist is there and launchd will load it."""
        return self.installed and not self.disabled

    @property
    def state(self) -> State:
        """What to tell the person. A leftover old-script agent comes first: it cannot
        start, so nothing else about it is true. Running means launchd has the job (this
        ``serve`` is that job, or another copy of it is). ``loaded`` counts because the CLI
        can never be the job, so it is the only signal there; in the web API, loaded without
        ``this_serve_is_the_agent`` means the agent is waiting behind this serve. A plist that
        runs another netkeep wins over Running: an old checkout's agent is usually loaded."""
        if self.legacy:
            return State.NEEDS_REPAIR
        if not self.enabled:
            return State.OFF
        if self.stale:
            return State.DIFFERENT_NETKEEP
        if self.this_serve_is_the_agent or self.loaded:
            return State.RUNNING
        return State.REGISTERED


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
    def data(self) -> Path:
        """The data directory this agent serves, resolved."""
        return self._data

    @property
    def executable(self) -> Path | None:
        """The ``netkeep`` a plist written now runs (:func:`installed_executable`)."""
        return self._executable

    def serve_address(self) -> tuple[str | None, int | None]:
        """The ``--host`` and ``--port`` the agent's ``serve`` runs with, or ``None`` for
        each one it leaves to the config. ``netkeep setup`` asks it where to look."""
        return self._kept_arguments(self._adoptable_legacy())

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
                legacy=False,
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
            legacy=self._adoptable_legacy() is not None,
        )

    def _adoptable_legacy(self) -> LegacyAgent | None:
        legacy = self._legacy()
        return legacy if legacy is not None and legacy.serves_here else None

    def _legacy(self) -> LegacyAgent | None:
        """An agent from before: the old script's, else one from before the rename (#661).

        The old script always used the plain label, whichever data directory it served,
        and ran ``serve-launchd.sh``. Before the rename, the login item's labels began
        ``fun.tnkr.netkeeper``, and the default data directory was
        ``.../Application Support/netkeeper``, which the rename moves to this one.
        """
        found = [
            self._old_plist(self._agents_dir / f"{LEGACY_LABEL_BASE}.plist", script=True),
            self._old_plist(self._agents_dir / f"{legacy_label_for(self._data)}.plist"),
        ]
        here = [legacy for legacy in found if legacy is not None and legacy.serves_here]
        return here[0] if here else next((legacy for legacy in found if legacy), None)

    def _old_plist(self, path: Path, *, script: bool = False) -> LegacyAgent | None:
        document = _read_plist(path)
        if document is None:
            return None
        arguments = _arguments(document)
        if not arguments or (Path(arguments[0]).name == WRAPPER_NAME) != script:
            return None
        env = document.get("EnvironmentVariables")
        named = None
        if isinstance(env, dict):
            named = env.get(paths.DATA_ENV) or env.get(LEGACY_DATA_ENV)
        serves = Path(named).expanduser() if isinstance(named, str) and named else None
        old_default = paths.legacy_default_data_dir()
        if serves is None:
            serves = old_default if old_default is not None else paths.default_data_dir()
        moved = (
            old_default is not None
            and _same_directory(serves, old_default)
            and paths.is_default_data_dir(self._data)
        )
        return LegacyAgent(
            path=path,
            label=path.name.removesuffix(".plist"),
            serves=serves,
            serves_here=moved or _same_directory(serves, self._data),
            arguments=arguments,
            environment={
                str(key): str(value)
                for key, value in (env.items() if isinstance(env, dict) else ())
                if isinstance(value, str)
            },
        )

    def legacy_agent(self) -> LegacyAgent | None:
        """The agent from before (:meth:`_legacy`) when it serves this data directory."""
        return self._adoptable_legacy()

    def legacy_enabled(self, legacy: LegacyAgent) -> bool:
        """Whether launchd would start ``legacy`` at login: not ``launchctl disable``-d."""
        return not self._is_disabled(legacy.label)

    def _plist_executable(self) -> str | None:
        document = _read_plist(self.plist_path)
        arguments = _arguments(document) if document is not None else None
        return arguments[0] if arguments else None

    def _kept_arguments(self, legacy: LegacyAgent | None) -> tuple[str | None, int | None]:
        """``--host`` and ``--port`` of the plist about to be replaced (this label's, else
        an adoptable old one), so a rewrite does not move the agent to the config's."""
        document = _read_plist(self.plist_path)
        arguments = _arguments(document) if document is not None else None
        if arguments is None and legacy is not None:
            arguments = legacy.arguments
        host: str | None = None
        port: int | None = None
        for flag, value in zip(arguments or [], (arguments or [])[1:], strict=False):
            if flag == "--host":
                host = value
            elif flag == "--port" and value.isdigit():
                port = int(value)
        return host, port

    def _is_loaded(self) -> bool:
        return self._runner(["print", self._service]).returncode == 0

    def _is_disabled(self, label: str | None = None) -> bool:
        done = self._runner(["print-disabled", self._domain])
        if done.returncode != 0:
            return False
        for line in done.stdout.splitlines():
            name, _, state = line.partition("=>")
            if name.strip().strip('"') == (self.label if label is None else label):
                return state.strip().rstrip(",") == "disabled"
        return False

    # -- the plist --------------------------------------------------------------------

    def render_plist(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        carried: Mapping[str, str] | None = None,
    ) -> bytes:
        if self._executable is None:
            raise LoginItemError(
                "Cannot find the installed netkeep command to run at login. Install"
                " netkeep (make install) and try again."
            )
        arguments = [str(self._executable), "serve"]
        if host:
            arguments += ["--host", host]
        if port is not None:
            arguments += ["--port", str(port)]
        env = {
            paths.DATA_ENV: str(self._data),
            "NETKEEP_LOG_STDERR_LEVEL": "WARNING",
        }
        # Settings that name where secrets and config live must follow the agent, or it
        # would read another Keychain service or config file than the serve you set up.
        # ``carried`` (the settings of the agent being replaced) wins over this process's.
        for name in (keychain.SERVICE_ENV, paths.CONFIG_ENV):
            value = (carried or {}).get(name) or (paths.getenv(name, self._environ) or "").strip()
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
            log_fd = os.open(self._logs / name, os.O_CREAT | os.O_APPEND | os.O_WRONLY, _LOG_MODE)
            try:
                os.fchmod(log_fd, _LOG_MODE)  # a file the old setup made keeps its old mode
            finally:
                os.close(log_fd)
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
        keep_arguments: bool = False,
    ) -> Outcome:
        """Install or replace the agent, adopting one the old script installed.

        ``serving_here`` says the caller is a ``serve`` itself (the Settings switch is):
        the lock is then held by definition. A held lock means the agent is registered
        and enabled but not started, so there is never a second ``serve``.
        ``keep_arguments`` keeps the ``--host`` and ``--port`` of the plist it replaces.

        An old plist serving this data directory (the old script's, which always used
        the plain old label, or one from before the rename, #661) is booted out and
        removed once the new one is in place, so two agents never fight over one serve
        lock. An old plist serving another data directory is never touched: since the
        rename its label is never this one's.
        """
        self._require_macos()
        adopting = self._adoptable_legacy()
        if keep_arguments and host is None and port is None:
            host, port = self._kept_arguments(adopting)
        carried = self._carried(adopting) if adopting is not None else None
        content = self.render_plist(host=host, port=port, carried=carried)
        previous = self.plist_path.read_bytes() if self.plist_path.is_file() else None
        self._write_plist(content)
        try:
            return self._load(
                changed=previous != content, serving_here=serving_here, adopting=adopting
            )
        except LoginItemError:
            if previous is None:  # leave the state as it was: nothing half-installed
                self.plist_path.unlink(missing_ok=True)
            else:
                self._write_plist(previous)
            raise

    def _load(self, *, changed: bool, serving_here: bool, adopting: LegacyAgent | None) -> Outcome:
        self._launchctl("enable", self._service)  # clears a `launchctl disable` from before
        me = self._environ.get("XPC_SERVICE_NAME")
        if adopting is not None and me == adopting.label:
            # This server is the old agent's job. Booting it out would end the server
            # answering, and the new agent cannot start under it (the serve lock).
            adopting.path.unlink(missing_ok=True)
            return Outcome(
                self.status(),
                started=False,
                message=(
                    "Repaired. This copy is still running under the old agent, which launchd"
                    " cannot restart now that the old script is gone. The repaired agent takes"
                    " over at your next login; to switch sooner, run netkeep login-item on in"
                    " a terminal."
                ),
            )
        loaded = self._is_loaded()
        if loaded and me == self.label:
            # This serve is the agent. Booting it out would stop the server answering.
            note = "The login item was already on and is running this server." + (
                " Its new settings apply the next time it starts." if changed else ""
            )
            return Outcome(self.status(), started=False, message=note)
        stopped = False
        if adopting is not None:
            stopped = self._stop(f"{self._domain}/{adopting.label}") or stopped
            adopting.path.unlink(missing_ok=True)
        if loaded:
            stopped = self._stop(self._service) or stopped
        if stopped and not serving_here:
            self._wait_until(lambda: not self._lock_held(), _LOCK_WAIT_S)
        if serving_here or self._lock_held():
            if serving_here:
                why = "netkeep is already running"
            else:
                why = "the previous copy of netkeep has not finished stopping"
            return Outcome(
                self.status(),
                started=False,
                message=(
                    "Registered. netkeep will start by itself the next time you log in to this"
                    f" Mac. Nothing changes now, because {why}: starting another copy would only"
                    " fail against the serve lock."
                ),
            )
        self._launchctl("bootstrap", self._domain, str(self.plist_path))
        return Outcome(
            self.status(),
            started=True,
            message=(
                "Started. netkeep is starting now, and will start by itself every time you log in"
                " to this Mac."
            ),
        )

    def _stop(self, service: str) -> bool:
        """Boot out ``service`` if launchd has it loaded; whether it was."""
        if self._runner(["print", service]).returncode != 0:
            return False
        self._launchctl("bootout", service)
        self._wait_until(lambda: self._runner(["print", service]).returncode != 0, _STOP_WAIT_S)
        return True

    def agent_pid(self) -> int | None:
        """The pid of the agent's running ``serve``, from ``launchctl print``; ``None`` when
        launchd has no job loaded or the job has no process (waiting to restart)."""
        done = self._runner(["print", self._service])
        if done.returncode != 0:
            return None
        for line in done.stdout.splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "pid" and value.strip().isdigit():
                return int(value.strip())
        return None

    def stop(self) -> bool:
        """Stop the agent's ``serve`` now and leave the plist, so it still starts at login
        and :meth:`enable` starts it again (``netkeep update``, #685). Whether it was
        loaded."""
        self._require_macos()
        return self._stop(self._service)

    def disable(self, *, serving_here: bool = False) -> Outcome:
        """Stop the agent and remove its plist, and an old script's for this data
        directory. Data and logs stay.

        When this ``serve`` is the agent (``serving_here``), stopping it ends the server
        answering, so the stop runs a second after the reply.
        """
        self._require_macos()
        legacy = self._adoptable_legacy()
        services = [self._service]
        if legacy is not None:
            services.append(f"{self._domain}/{legacy.label}")
        me = self._environ.get("XPC_SERVICE_NAME")
        loaded = [
            service for service in services if self._runner(["print", service]).returncode == 0
        ]
        # The plists go first: if a stop below fails, the item is still off at login.
        self.plist_path.unlink(missing_ok=True)
        if legacy is not None:
            legacy.path.unlink(missing_ok=True)
        message = "Turned off. netkeep will not start at login."
        for service in loaded:
            if serving_here and service.rsplit("/", 1)[-1] == me:
                self._defer(functools.partial(self._bootout_quietly, service))
                message = (
                    "Turned off. This copy of netkeep was started by the login item, so it"
                    " stops in a moment. Run netkeep serve to start it again."
                )
            else:
                self._stop(service)
        return Outcome(self.status(), started=False, message=message)

    def _carried(self, legacy: LegacyAgent) -> dict[str, str]:
        """The old agent's Keychain service and config settings, under their new names,
        with a path inside the moved default folder pointed at the new one (#661)."""
        carried: dict[str, str] = {}
        old_default = paths.legacy_default_data_dir()
        for name in (keychain.SERVICE_ENV, paths.CONFIG_ENV):
            value = paths.getenv(name, legacy.environment)
            if not value or not value.strip():
                continue
            value = value.strip()
            if name == paths.CONFIG_ENV and old_default is not None:
                candidate = Path(value).expanduser()
                if paths.is_default_data_dir(self._data) and _within(candidate, old_default):
                    value = str(self._data / candidate.resolve().relative_to(old_default.resolve()))
            carried[name] = value
        return carried

    def replace_old_job(self) -> str | None:
        """When this process is an agent from before the rename (#661), start the new one
        and stop the old, whose definition points into the moved folder. Returns what it
        did, or None when this is not such an agent. Stopping the old job ends this process
        under launchd, so the caller has finished everything else first."""
        self._require_macos()
        me = self._environ.get("XPC_SERVICE_NAME", "")
        if me != LEGACY_LABEL_BASE and not me.startswith(f"{LEGACY_LABEL_BASE}."):
            return None
        started = False
        if self.plist_path.is_file():
            self._launchctl("enable", self._service)
            if not self._is_loaded():
                self._launchctl("bootstrap", self._domain, str(self.plist_path))
            started = True
        self._runner(["bootout", f"{self._domain}/{me}"])
        if started:
            return f"handed over from the login item {me} to {self.label}, which starts now"
        return f"stopped the login item {me}, which was off"

    def retire_legacy(self) -> None:
        """Remove the old agent for this data directory (:meth:`legacy_agent`) and stop
        it if launchd has it loaded, unless it is this process. The new one is untouched:
        the rename's migration (#661) uses this when the old agent was off."""
        self._require_macos()
        legacy = self._adoptable_legacy()
        if legacy is None:
            return
        legacy.path.unlink(missing_ok=True)
        if self._environ.get("XPC_SERVICE_NAME") != legacy.label:
            self._stop(f"{self._domain}/{legacy.label}")

    # -- helpers ----------------------------------------------------------------------

    def _bootout_quietly(self, service: str) -> None:
        try:
            self._runner(["bootout", service])
        except LoginItemError:
            log.warning("could not stop the login item", exc_info=True)

    def _launchctl(self, *args: str) -> None:
        done = self._runner(list(args))
        if done.returncode != 0:
            detail = (done.stderr or done.stdout).strip() or f"exit status {done.returncode}"
            raise LoginItemError(f"launchctl {args[0]} failed: {detail}")

    def _wait_until(self, condition: Callable[[], bool], budget_s: float) -> bool:
        """bootout returns before the job is gone, and a bootstrap that races it fails.
        Whether ``condition`` held within ``budget_s``."""
        for _ in range(int(budget_s / _POLL_S)):
            if condition():
                return True
            self._sleep(_POLL_S)
        return condition()

    def _require_macos(self) -> None:
        if not self._macos:
            raise UnsupportedPlatform(UNSUPPORTED_REASON)


@dataclass(frozen=True, slots=True)
class LegacyAgent:
    """A plist the removed ``install-launchd.sh`` wrote, or one from before the rename."""

    path: Path
    label: str
    serves: Path
    serves_here: bool
    arguments: list[str]
    environment: dict[str, str] = field(default_factory=dict)
    """Its ``EnvironmentVariables``, carried over when it is replaced (#661)."""


def _read_plist(path: Path) -> dict[str, object] | None:
    try:
        with path.open("rb") as handle:
            document = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    return document if isinstance(document, dict) else None


def _arguments(document: Mapping[str, object]) -> list[str] | None:
    arguments = document.get("ProgramArguments")
    if isinstance(arguments, list) and arguments and all(isinstance(a, str) for a in arguments):
        return [str(a) for a in arguments]
    return None


def _within(path: Path, folder: Path) -> bool:
    path, folder = path.expanduser().resolve(), folder.expanduser().resolve()
    return path == folder or folder in path.parents


def _same_directory(left: Path, right: Path) -> bool:
    left, right = left.expanduser().resolve(), right.expanduser().resolve()
    if left == right:
        return True
    try:
        return left.samefile(right)
    except OSError:
        return False


def _serve_lock_held() -> bool:
    return serve_lock.state().held


def _defer_a_second(action: Callable[[], None]) -> None:
    timer = threading.Timer(_DEFERRED_STOP_S, action)
    timer.daemon = True
    timer.start()
