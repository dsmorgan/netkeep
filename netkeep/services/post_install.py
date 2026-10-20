"""``netkeep setup``: what the installer runs after it installs netkeep (ADR 0011, rules 6
and 7, #684).

Three steps, each reported as it ends, and a stop at the first that fails, with what to
do about it:

1. **The login item**, through :class:`~netkeep.services.login_item.LoginItem`, the code
   behind the Settings switch and ``netkeep login-item on``. Its plist runs the stable
   installed ``netkeep`` (:func:`~netkeep.services.login_item.installed_executable`):
   under ``uv tool install``, uv's shim such as ``~/.local/bin/netkeep``, never the tool
   environment's own ``bin``. From a checkout's ``.venv``, it runs that one, and setup
   says so.
2. **serve answers.** It polls ``GET /api/v1/health`` on loopback, and only on loopback,
   until the agent's ``serve`` answers or the wait runs out.
3. **The first-run page** in netkeep's Chrome, through the one launcher (#658), the way
   setup's first-run button opens it: the UI's window and LinkedIn's sign-in page. The
   CLI hands this module the launch, which is ``netkeep open``'s launcher with
   ``first_run=True``; nothing here imports the launcher (``tests/test_browser_safety.py``).

**Run again,** it changes nothing that is already right: a login item that is on, runs
this ``netkeep`` and is loaded is left alone (re-enabling would restart its ``serve``), and
while netkeep's Chrome is running, it opens no other window and points at
``netkeep open``. **The data directory** is the one every command uses
(:func:`netkeep.paths.data_dir`): setup never chooses, moves or migrates it beyond the
CLI's own startup hook (rule 7). **--remove** turns the login item off and leaves all data.
"""

from __future__ import annotations

import http.client
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit, urlunsplit
from urllib.request import ProxyHandler, build_opener

from netkeep.services.chrome_launch import LaunchResult, LaunchStatus, Refusal
from netkeep.services.login_item import SERVE_ERR_LOG, LoginItem, LoginItemError

#: How long setup waits for the agent's ``serve`` to answer. A first start creates the
#: database and runs its migrations before it listens.
SERVE_WAIT_S: Final = 60.0

HEALTH_PATH: Final = "/api/v1/health"
_HEALTH_TIMEOUT_S: Final = 2.0
_POLL_S: Final = 0.5
_LOOPBACK: Final = frozenset({"127.0.0.1", "localhost", "::1"})

#: What to do about each refusal of the launch, after the launcher's own message.
REFUSAL_FIXES: Final[dict[Refusal, str]] = {
    Refusal.NOT_DEDICATED: (
        "Another Chrome holds netkeep's debugging port. Quit it, then run netkeep setup again."
    ),
    Refusal.PROFILE_WITHOUT_PORT: (
        "netkeep's Chrome profile is open without its debugging port. Quit that Chrome, then"
        " run netkeep setup again."
    ),
    Refusal.RUN_HOLDS_BROWSER: (
        "A LinkedIn run holds the browser. Wait for it to end, then run netkeep setup again."
    ),
    Refusal.REMOTE_ADDRESS: (
        "linkedin.cdp_url names another host, where netkeep can't start Chrome. Point it at"
        " 127.0.0.1 in Settings or your config, then run netkeep setup again."
    ),
    Refusal.INVALID_PORT: (
        "linkedin.cdp_url names no valid port. Fix it in Settings or your config, then run"
        " netkeep setup again."
    ),
}


class StepFailed(Exception):
    """A step can't finish. ``message`` says what happened; ``fix`` what to do about it."""

    def __init__(self, message: str, fix: str) -> None:
        super().__init__(message)
        self.message = message
        self.fix = fix


Say = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class Seams:
    """What setup reaches the world through; the CLI passes the real ones, tests fakes."""

    base_url: Callable[[str | None, int | None], str]
    """The UI's origin for the agent's ``--host`` and ``--port`` (``None``: the config's)."""
    open_first_run: Callable[[str], LaunchResult]
    """Opens the first-run page for that origin in netkeep's Chrome. May raise
    :class:`StepFailed` (the Keychain refused the key the link needs)."""
    chrome_pid: Callable[[], int | None]
    """The live process holding netkeep's Chrome profile, or ``None``."""
    serve_answers: Callable[[str], bool]
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic


def run(item: LoginItem, seams: Seams, say: Say, *, tool_install: bool, wait_s: float) -> int:
    """Run the three steps; the exit status: 0 when every one is done."""
    say(f"netkeep setup, for the data directory {item.data}")
    try:
        _login_item(item, say, tool_install=tool_install)
        base = _serve(item, seams, say, wait_s=wait_s)
        _first_run(seams, say, base)
    except StepFailed as exc:
        say(f"   failed: {exc.message}")
        say(f"   to fix it: {exc.fix}")
        return 1
    say("")
    say("netkeep is set up. It starts by itself when you log in to this Mac.")
    say("To turn that off and keep your data, run: netkeep setup --remove")
    return 0


def remove(item: LoginItem, say: Say) -> int:
    """``--remove``: turn the login item off. Data, logs and Keychain items stay."""
    say(f"netkeep setup --remove, for the data directory {item.data}")
    status = item.status()
    if not status.supported:
        say(f"   failed: {status.reason}")
        return 1
    if not (status.installed or status.loaded or status.legacy):
        say(f"   already done: the login item {status.label} is off.")
    else:
        try:
            outcome = item.disable()
        except LoginItemError as exc:
            say(f"   failed: {exc}")
            say(
                "   to fix it: run netkeep login-item status to see where it stands, then run"
                " netkeep setup --remove again."
            )
            return 1
        say(f"   done: {outcome.message}")
    say(f"Your data stays in {item.data}. Nothing was deleted.")
    return 0


def _login_item(item: LoginItem, say: Say, *, tool_install: bool) -> None:
    say("1. Turn on the login item, so netkeep serve starts when you log in")
    status = item.status()
    if not status.supported:
        raise StepFailed(
            status.reason or "the login item isn't available here",
            "Run netkeep serve yourself, or run netkeep in its container.",
        )
    executable = item.executable
    if executable is None:
        raise StepFailed(
            "can't find the installed netkeep command for the login item to run.",
            "Install netkeep with the installer in the README, then run netkeep setup again.",
        )
    if not tool_install:
        say(
            f"   note: this netkeep runs from {executable}, not from a uv tool install (a"
            " checkout's .venv, for example). The login item runs it from there, so it stops"
            " working if you move or delete that folder."
        )
    if status.enabled and status.loaded and not status.stale and not status.legacy:
        say(
            f"   already done: {status.label} runs {executable} serve at login, and launchd"
            " has it loaded."
        )
        return
    try:
        outcome = item.enable(keep_arguments=True)
    except LoginItemError as exc:
        raise StepFailed(
            str(exc),
            "run netkeep login-item status to see where it stands, then run netkeep setup again.",
        ) from exc
    say(f"   done: {outcome.message}")
    say(f"   {outcome.status.label} runs {executable} serve.")


def _serve(item: LoginItem, seams: Seams, say: Say, *, wait_s: float) -> str:
    host, port = item.serve_address()
    base = seams.base_url(host, port)
    say(f"2. Wait for netkeep serve to answer at {base}")
    deadline = seams.clock() + wait_s
    while not seams.serve_answers(base):
        if seams.clock() >= deadline:
            errors = item.data / "logs" / SERVE_ERR_LOG
            raise StepFailed(
                f"netkeep serve didn't answer at {base}{HEALTH_PATH} within {wait_s:g} s.",
                f"Look in {errors} for why it didn't start, and run netkeep login-item"
                " status to see whether launchd has it loaded. Then run netkeep setup again.",
            )
        seams.sleep(_POLL_S)
    say("   done: netkeep serve answers.")
    return base


def _first_run(seams: Seams, say: Say, base: str) -> None:
    say("3. Open the first-run page in netkeep's Chrome")
    pid = seams.chrome_pid()
    if pid is not None:
        say(
            f"   already done: netkeep's Chrome is running (pid {pid}), so setup opens no"
            " other window. To bring up the UI, run netkeep open."
        )
        return
    result = seams.open_first_run(base)
    if result.reached_chrome:
        say(f"   done: {result.message}.")
        if result.status is LaunchStatus.HANDOFF_UNCONFIRMED:
            say("   If no netkeep window opened, run netkeep open.")
        return
    fix = REFUSAL_FIXES.get(result.refusal) if result.refusal is not None else None
    raise StepFailed(
        f"netkeep's Chrome: {result.message}.",
        fix or "Fix what it says, then run netkeep setup again, or run netkeep open.",
    )


def serve_answers(base: str, *, timeout_s: float = _HEALTH_TIMEOUT_S) -> bool:
    """Whether a netkeep ``serve`` answers ``GET /api/v1/health`` at ``base``.

    Only an ``http`` origin on a loopback name is asked; any other is never connected to.
    """
    parts = urlsplit(base)
    host = parts.hostname
    if parts.scheme != "http" or host not in _LOOPBACK:
        return False
    try:
        parts.port  # noqa: B018 -- raises for a port out of range
    except ValueError:
        return False
    # No proxy: a loopback address is asked directly, whatever the environment says.
    opener = build_opener(ProxyHandler({}))
    url = urlunsplit(("http", parts.netloc, HEALTH_PATH, "", ""))
    try:
        with opener.open(url, timeout=timeout_s) as response:
            if response.status != 200:
                return False
            body = json.loads(response.read(4096))
    except (OSError, http.client.HTTPException, ValueError):
        return False
    return isinstance(body, dict) and body.get("status") == "ok"
