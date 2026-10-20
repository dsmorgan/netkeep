"""``netkeep update``: upgrade a uv tool install in place, with a backup and a rollback
(ADR 0011, rules 1, 8 and 9; #685).

Three processes, each running code nothing has replaced under it:

1. **``netkeep update``**, on the installed version. It finds the target (the latest
   GitHub release, or ``--version``), downloads both versions' ``constraints.txt`` and
   checks each against its release's ``SHA256SUMS``, and keeps them in
   ``<data>/update/``. It refuses while a ``serve`` runs that isn't the login item's,
   stops the login item, holds the serve lock so no ``serve`` starts meanwhile, backs up
   the database, writes :data:`STATE_FILE`, runs ``uv tool install`` for the target, and
   executes the new ``netkeep update --finish``.
2. **``netkeep update --finish``**, on the new version. It starts the login item and
   waits for ``serve`` to answer its health check with the new version. Then the update
   is done. If it fails, it stops the login item and takes the serve lock. If no
   ``serve`` started a scheduler during the update (:data:`SCHEDULER_MARKER`, any
   version's), it reinstalls the old version with its constraints, still holding the
   lock, and executes the old ``netkeep update --rollback``. Otherwise it turns the
   login item off and explains: a scheduler may have sent messages, and restoring the
   backup would forget them and risk sending them twice.
3. **``netkeep update --rollback``**, on the old version again. Holding the serve lock,
   it checks the marker itself (a ``serve`` may have run since ``--finish``) and stops
   and explains if one exists. Otherwise it records the restore in :data:`STATE_FILE`,
   restores the backup (when the new version ran on the database), starts the login
   item, and checks ``serve`` answers with the old version. A second ``--rollback``
   never restores again.

The principle: a restore never happens after any scheduler may have run on the data.
:data:`STATE_FILE`, the marker and the constraints files are written atomically and
durably (file and folder fsynced), and ``serve`` starts its scheduler only once the marker
is on disk. Each version reads a :data:`STATE_FILE` another wrote, so its fields and
phases are frozen and keys a version doesn't know are kept (#705).

When ``uv tool install`` fails, the new version has never run, so the old process
reinstalls the old version and executes its ``--rollback``, which has no database to
restore. netkeep never updates itself silently: ``--check`` only says whether a newer
release exists. A checkout install is refused: ``git pull`` and ``make install`` update it.

Everything outside this module goes through :class:`Seams`: the CLI passes the real
downloads, ``uv``, ``exec``, backup, restore and health check, and tests pass fakes.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import ssl
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, NoReturn
from urllib.parse import urljoin, urlsplit

from netkeep import paths
from netkeep.fsutil import sync_directory, write_atomic
from netkeep.linkedin.activity_lock import Claim, LockState
from netkeep.services.login_item import SERVE_ERR_LOG, LoginItem, LoginItemError

REPOSITORY: Final = "dsmorgan/netkeep"
LATEST_URL: Final = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
DOWNLOAD_URL: Final = f"https://github.com/{REPOSITORY}/releases/download"
#: The hosts a release download may reach: the API, the release page, and the storage
#: GitHub redirects a release asset to. A redirect anywhere else is refused.
ALLOWED_HOSTS: Final = frozenset(
    {
        "api.github.com",
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
)
_MAX_REDIRECTS: Final = 5
_DOWNLOAD_TIMEOUT_S: Final = 30.0
_MAX_RELEASE_BYTES: Final = 1 << 20
_MAX_ASSET_BYTES: Final = 1 << 20
_DOWNLOAD_DEADLINE_S: Final = 120.0
"""The whole download, redirects included: a server that drips the body can't hold it."""
_CHUNK_BYTES: Final = 64 * 1024

UPDATE_DIRNAME: Final = "update"
STATE_FILE: Final = "state.json"
"""The update in progress: what the three processes hand each other."""
SCHEDULER_MARKER: Final = "scheduler-started.json"
"""What ``serve`` writes, while an update is in progress, before it starts its scheduler."""

SERVE_WAIT_S: Final = 90.0
"""How long ``--finish`` and ``--rollback`` wait for ``serve`` to answer: a first start
on a new version runs its migrations before it listens."""
_POLL_S: Final = 0.5
_LOCK_WAIT_S: Final = 60.0
"""How long the serve lock may stay held after the login item is stopped: a serve's
shutdown can wait 45 s for a send."""

_VERSION: Final = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")


class UpdateError(Exception):
    """The update can't go on; the message says why, and nothing was changed unless it
    says so."""


Say = Callable[[str], None]


def parse_version(text: str) -> tuple[int, int, int] | None:
    """``X.Y.Z`` as three numbers, else ``None``: releases are plain ``vX.Y.Z`` tags."""
    match = _VERSION.match(text)
    if match is None:
        return None
    return int(match[1]), int(match[2]), int(match[3])


def update_dir(data: Path | None = None) -> Path:
    return (paths.data_dir() if data is None else data) / UPDATE_DIRNAME


def constraints_path(version: str, data: Path | None = None) -> Path:
    """Where each install's constraints file is kept (ADR 0011, rule 8)."""
    return update_dir(data) / f"constraints-{version}.txt"


# -- downloads -------------------------------------------------------------------------


def https_get(
    url: str,
    *,
    max_bytes: int,
    accept: str = "*/*",
    clock: Callable[[], float] = time.monotonic,
) -> bytes:
    """GET ``url`` over HTTPS with certificate checks, following redirects only to HTTPS
    on :data:`ALLOWED_HOSTS` at port 443, with no proxy, at most ``max_bytes`` of the
    body, and within :data:`_DOWNLOAD_DEADLINE_S` for the whole download, redirects
    included. :class:`UpdateError` on anything else."""
    context = ssl.create_default_context()
    deadline = clock() + _DOWNLOAD_DEADLINE_S
    for _ in range(_MAX_REDIRECTS + 1):
        parts = urlsplit(url)
        host = parts.hostname
        try:
            port = parts.port
        except ValueError:
            port = -1
        if parts.scheme != "https" or host not in ALLOWED_HOSTS or port not in (None, 443):
            raise UpdateError(f"refused to download {url}: only HTTPS from GitHub is allowed")
        connection = http.client.HTTPSConnection(
            host, 443, timeout=_DOWNLOAD_TIMEOUT_S, context=context
        )
        try:
            target = parts.path + (f"?{parts.query}" if parts.query else "")
            connection.request(
                "GET",
                target or "/",
                headers={"Accept": accept, "User-Agent": f"netkeep-update ({REPOSITORY})"},
            )
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location:
                    raise UpdateError(f"{url} redirected nowhere")
                url = urljoin(url, location)
                _check_deadline(url, deadline, clock)
                continue
            if response.status != 200:
                raise UpdateError(f"could not download {url}: HTTP {response.status}")
            chunks: list[bytes] = []
            size = 0
            while size <= max_bytes:
                _check_deadline(url, deadline, clock)
                chunk = response.read(min(_CHUNK_BYTES, max_bytes + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            body = b"".join(chunks)
        except (OSError, http.client.HTTPException) as exc:
            raise UpdateError(f"could not download {url}: {exc}") from exc
        finally:
            connection.close()
        if len(body) > max_bytes:
            raise UpdateError(f"{url} is larger than {max_bytes} bytes")
        return body
    raise UpdateError(f"{url} redirected more than {_MAX_REDIRECTS} times")


def _check_deadline(url: str, deadline: float, clock: Callable[[], float]) -> None:
    if clock() > deadline:
        raise UpdateError(
            f"could not download {url}: it took longer than {_DOWNLOAD_DEADLINE_S:g} s"
        )


def sha256_for(sums: str, name: str) -> str:
    """The one checksum ``SHA256SUMS`` gives ``name``. :class:`UpdateError` when there is
    none, more than one, or one that isn't 64 hex digits."""
    found = []
    for line in sums.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1].removeprefix("*") == name:
            found.append(fields[0])
    if len(found) != 1 or not _SHA256.match(found[0]):
        raise UpdateError(f"SHA256SUMS has no single valid checksum for {name}")
    return found[0]


# -- the state the three processes share -----------------------------------------------


PHASES: Final = frozenset({"installing", "finishing", "rolling-back", "stopped"})


@dataclass(frozen=True, slots=True)
class UpdateState:
    """:data:`STATE_FILE`. ``phase`` is the step under way: ``installing`` (the old
    version runs ``uv``), ``finishing`` (the new version checks itself), ``rolling-back``,
    or ``stopped`` (stop-and-explain: a person decides what's next)."""

    from_version: str
    to_version: str
    from_constraints: str
    to_constraints: str
    backup: str | None
    login_item_was_on: bool
    phase: str
    new_version_ran: bool = False
    """The new version's ``serve`` was started, so it may have migrated the database."""
    restored: bool = False
    """``--rollback`` began restoring the backup. Recorded before the restore and before
    ``serve`` starts again, so a second ``--rollback`` never restores over what the
    restored version did."""
    reason: str = ""
    """Why it is rolling back or stopped."""
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    carried: dict[str, object] = field(default_factory=dict, compare=False, repr=False)
    """Keys in :data:`STATE_FILE` this version doesn't know, written back unchanged: a
    newer version wrote them, and the old version's ``--rollback`` hands the file on."""


# The compatibility rule across versions (#705). An update runs two versions on one
# STATE_FILE: the new version's --finish writes it and the old version's --rollback reads
# it. So the fields below and PHASES are frozen: no version renames, removes, retypes or
# changes the meaning of one, and tests/test_update.py pins them. A later version may add a
# field, with a default, that an older version can safely ignore; anything a rollback must
# honor to stay safe (the scheduler marker, restored) is already here. A key this version
# doesn't know is kept and written back (UpdateState.carried); a known key of the wrong
# type, or a phase this version doesn't know, is still refused, and a refusal never
# restores.
_STATE_TYPES: Final[dict[str, tuple[type, ...]]] = {
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
}


def read_state(data: Path | None = None) -> UpdateState | None:
    """The update in progress, or ``None``. :class:`UpdateError` for a file that isn't
    one a netkeep wrote: a field of the wrong type or missing, a version that isn't
    ``X.Y.Z`` (it goes to ``uv``), or a phase it doesn't know. Keys it doesn't know are
    kept in :attr:`UpdateState.carried` (the rule above :data:`_STATE_TYPES`)."""
    path = update_dir(data) / STATE_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise UpdateError(f"{path} can't be read ({exc}); look at it, then delete it") from exc
    unknown = f"{path} isn't an update netkeep knows; look at it, then delete it"
    if not isinstance(raw, dict):
        raise UpdateError(unknown)
    known = {name: value for name, value in raw.items() if name in _STATE_TYPES}
    carried = {name: value for name, value in raw.items() if name not in _STATE_TYPES}
    if any(not isinstance(value, _STATE_TYPES[name]) for name, value in known.items()):
        raise UpdateError(unknown)
    try:
        state = UpdateState(**known, carried=carried)
    except TypeError as exc:
        raise UpdateError(unknown) from exc
    if (
        parse_version(state.from_version) is None
        or parse_version(state.to_version) is None
        or state.phase not in PHASES
    ):
        raise UpdateError(unknown)
    # Each constraints file is the one this update kept, never a path the file names.
    for version, recorded in (
        (state.from_version, state.from_constraints),
        (state.to_version, state.to_constraints),
    ):
        if Path(recorded).resolve() != constraints_path(version, data).resolve():
            raise UpdateError(unknown)
    return state


def _write_durably(path: Path, text: str) -> None:
    """Write ``path`` so that it is on disk when this returns (:func:`write_atomic`)."""
    write_atomic(path, text)


def _update_dir_made(data: Path | None) -> Path:
    directory = update_dir(data)
    if not directory.is_dir():
        directory.mkdir(parents=True, exist_ok=True)
        sync_directory(directory.parent)
    return directory


def write_state(state: UpdateState, data: Path | None = None) -> None:
    """Record ``state``, durably: a later process (or ``serve``, which writes the
    scheduler marker only while it exists) must find it after a power loss."""
    path = _update_dir_made(data) / STATE_FILE
    known = {name: value for name, value in asdict(state).items() if name in _STATE_TYPES}
    _write_durably(path, json.dumps(state.carried | known, indent=2) + "\n")


def clear_state(data: Path | None = None) -> None:
    directory = update_dir(data)
    (directory / STATE_FILE).unlink(missing_ok=True)
    (directory / SCHEDULER_MARKER).unlink(missing_ok=True)
    sync_directory(directory)


def clear_marker(data: Path | None = None) -> None:
    directory = update_dir(data)
    (directory / SCHEDULER_MARKER).unlink(missing_ok=True)
    sync_directory(directory)


def mark_scheduler_starting(version: str, data: Path | None = None) -> None:
    """``serve`` calls this before it starts its scheduler. While an update is in progress
    it records, durably, that a scheduler may now run on this data directory, so no
    rollback restores a backup over what it sent. Otherwise it writes nothing. It
    returns only once the marker is on disk; an error writing it is raised, so ``serve``
    doesn't start the scheduler unrecorded."""
    directory = update_dir(data)
    if not (directory / STATE_FILE).exists():
        return
    marker = {"version": version, "pid": os.getpid(), "at": datetime.now(UTC).isoformat()}
    _write_durably(directory / SCHEDULER_MARKER, json.dumps(marker) + "\n")


def scheduler_started(data: Path | None = None) -> bool:
    """Whether a ``serve`` recorded that it was starting its scheduler during this update.
    Any marker counts, whatever version it names and whatever it holds: a scheduler that
    ran on this data directory may have sent, and a restore would forget it."""
    return (update_dir(data) / SCHEDULER_MARKER).exists()


# -- the seams -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Seams:
    """What an update reaches the world through."""

    fetch: Callable[[str, int], bytes]
    """GET a URL over HTTPS, at most that many bytes (:func:`https_get`)."""
    uv: Callable[[list[str]], int]
    """Run ``uv`` with these arguments, its output shown; the exit status."""
    tool_bin: Callable[[], Path | None]
    """``uv tool dir --bin``: where uv puts the ``netkeep`` it installs."""
    exec_netkeep: Callable[[Path, list[str]], NoReturn]
    """Replace this process with that ``netkeep`` and these arguments."""
    serve_version: Callable[[str], str | None]
    """The version a ``serve`` reports at that origin, or ``None`` when none answers."""
    base_url: Callable[[str | None, int | None], str]
    lock_state: Callable[[], LockState]
    claim_lock: Callable[[], Claim]
    """Take the serve lock; raises when it is held."""
    backup: Callable[[], Path | None]
    """Back the database up; the file, or ``None`` when there is no database yet."""
    restore: Callable[[Path], list[str]]
    """Restore that backup (the serve lock held); what it did, in lines."""
    tool_install: bool
    """This netkeep is a uv tool install (not a checkout's ``.venv``)."""
    current_version: str
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic


CHECKOUT_REFUSAL: Final = (
    "this netkeep runs from a checkout, not a uv tool install, so netkeep update can't"
    " replace it. Update a checkout with: git pull, then make install."
)


def latest_version(seams: Seams) -> str:
    """The newest GitHub release's version."""
    body = seams.fetch(LATEST_URL, _MAX_RELEASE_BYTES)
    try:
        release = json.loads(body)
    except ValueError as exc:
        raise UpdateError(f"GitHub's answer for the latest release isn't JSON: {exc}") from exc
    tag = release.get("tag_name") if isinstance(release, dict) else None
    if not isinstance(tag, str) or parse_version(tag.removeprefix("v")) is None:
        raise UpdateError(f"the latest release's tag isn't vX.Y.Z: {tag!r}")
    return tag.removeprefix("v")


def download_constraints(seams: Seams, version: str, data: Path) -> Path:
    """``version``'s ``constraints.txt``, checked against its ``SHA256SUMS`` and kept as
    :func:`constraints_path`. Nothing is kept when the check fails."""
    base = f"{DOWNLOAD_URL}/v{version}"
    constraints = seams.fetch(f"{base}/constraints.txt", _MAX_ASSET_BYTES)
    sums = seams.fetch(f"{base}/SHA256SUMS", _MAX_ASSET_BYTES)
    try:
        expected = sha256_for(sums.decode("utf-8"), "constraints.txt")
    except UnicodeDecodeError as exc:
        raise UpdateError(f"{version}'s SHA256SUMS isn't text") from exc
    actual = hashlib.sha256(constraints).hexdigest()
    if actual != expected:
        raise UpdateError(
            f"{version}'s constraints.txt has SHA-256 {actual}, but its SHA256SUMS says"
            f" {expected}. Nothing was changed."
        )
    target = constraints_path(version, data)
    # Durably, and never half-written: a rollback reinstalls the old version from it.
    write_atomic(_update_dir_made(data) / target.name, constraints, mode=0o644)
    return target


def install_command(version: str, constraints: Path) -> list[str]:
    """The ``uv`` arguments that install ``version`` with its constraints (ADR 0011,
    rule 1). ``--reinstall`` rebuilds the tool's environment from nothing."""
    return [
        "tool",
        "install",
        "--python",
        "3.12",
        "--managed-python",
        "-c",
        str(constraints),
        f"netkeep=={version}",
        "--reinstall",
    ]


# -- netkeep update --check ------------------------------------------------------------


def check(seams: Seams, say: Say, *, version: str | None = None) -> int:
    """Say whether a newer release exists. Changes nothing."""
    if version is not None and parse_version(version) is None:
        say(f"error: --version takes X.Y.Z, not {version!r}.")
        return 1
    try:
        target = version or latest_version(seams)
    except UpdateError as exc:
        say(f"error: {exc}")
        return 1
    if _newer(target, seams.current_version):
        say(f"netkeep {target} is available; this is {seams.current_version}.")
        say("To update, run: netkeep update")
    else:
        say(f"netkeep {seams.current_version} is up to date (the latest release is {target}).")
    return 0


def _newer(target: str, current: str) -> bool:
    left, right = parse_version(target), parse_version(current)
    return left is not None and right is not None and left > right


# -- netkeep update --------------------------------------------------------------------


def start(item: LoginItem, seams: Seams, say: Say, *, version: str | None = None) -> int:
    """Step 1, on the installed version. Returns an exit status, or doesn't return: it
    executes the new ``netkeep update --finish``."""
    data = item.data
    if not seams.tool_install:
        say(f"error: {CHECKOUT_REFUSAL}")
        return 1
    current = seams.current_version
    if parse_version(current) is None:
        say(f"error: this netkeep's version, {current}, isn't a release, so it can't be updated.")
        return 1
    try:
        pending = read_state(data)
    except UpdateError as exc:
        say(f"error: {exc}")
        return 1
    if pending is not None:
        say(
            f"error: an update from {pending.from_version} to {pending.to_version} didn't"
            f" finish ({pending.phase}). {pending.reason}".rstrip()
        )
        say(
            f"Once netkeep is working the way you want, delete {update_dir(data) / STATE_FILE}"
            " and run netkeep update again (docs/guide/troubleshooting.md, Updating)."
        )
        return 1
    if version is not None and parse_version(version) is None:
        say(f"error: --version takes X.Y.Z, not {version!r}.")
        return 1
    try:
        target = version or latest_version(seams)
    except UpdateError as exc:
        say(f"error: {exc}")
        return 1
    if not _newer(target, current):
        say(f"netkeep {current} is up to date; nothing to do (asked for {target}).")
        return 0
    say(f"netkeep update: {current} to {target}, for the data directory {data}")

    say("1. Download both versions' constraints files and check their checksums")
    try:
        old_constraints = download_constraints(seams, current, data)
        new_constraints = download_constraints(seams, target, data)
    except UpdateError as exc:
        say(f"   failed: {exc}")
        return 1
    say(f"   done: kept in {update_dir(data)}")

    say("2. Stop netkeep serve")
    try:
        was_on, held = _stop_serve(item, seams, say)
    except UpdateError as exc:
        say(f"   failed: {exc}")
        return 1

    try:
        say("3. Back up the database")
        try:
            backup = seams.backup()
        except Exception as exc:  # any failure stops before a change
            raise _Abort(f"the backup failed: {exc}") from exc
        say(f"   done: {backup}" if backup is not None else "   done: no database yet")
        state = UpdateState(
            from_version=current,
            to_version=target,
            from_constraints=str(old_constraints),
            to_constraints=str(new_constraints),
            backup=None if backup is None else str(backup),
            login_item_was_on=was_on,
            phase="installing",
        )
        try:
            write_state(state, data)
            clear_marker(data)
        except OSError as exc:
            (update_dir(data) / STATE_FILE).unlink(missing_ok=True)
            raise _Abort(f"could not record the update in {update_dir(data)}: {exc}") from exc
    except _Abort as exc:
        held.release()
        say(f"   failed: {exc}")
        _restart(item, say, was_on)
        return 1

    say(f"4. Install netkeep {target}")
    status = seams.uv(install_command(target, new_constraints))
    if status != 0:
        reason = f"uv tool install of {target} failed (exit status {status})."
        say(f"   failed: {reason}")
        return _reinstall_old_and_rollback(item, seams, say, replace(state, reason=reason), held)
    held.release()
    try:
        write_state(replace(state, phase="finishing"), data)
    except OSError as exc:
        # The state still says installing, which --finish accepts on the new version.
        say(f"   could not record that the install finished ({exc}); going on")
    say("5. Hand over to the new version")
    _exec(seams, "--finish")


def _stop_serve(item: LoginItem, seams: Seams, say: Say) -> tuple[bool, Claim]:
    """Refuse while a ``serve`` that isn't the login item's runs; stop the login item;
    take the serve lock. Whether the login item was on, and the held lock."""
    status = item.status()
    lock = seams.lock_state()
    if lock.held:
        holder = lock.holder.pid if lock.holder is not None else None
        agent = item.agent_pid() if status.supported else None
        if holder is None or agent is None or holder != agent:
            who = lock.holder.describe() if lock.holder is not None else "another netkeep process"
            raise UpdateError(
                f"{who} holds the serve lock, and it isn't the login item's serve. Stop that"
                " netkeep serve (or wait for the netkeep restore to finish), then run"
                " netkeep update again."
            )
    was_on = status.supported and status.enabled and not status.legacy
    if status.supported and status.loaded:
        try:
            item.stop()
        except LoginItemError as exc:
            raise UpdateError(f"could not stop the login item: {exc}") from exc
        say(f"   stopped the login item {status.label}")
    deadline = seams.clock() + _LOCK_WAIT_S
    while seams.lock_state().held:
        if seams.clock() >= deadline:
            _restart(item, say, was_on)
            raise UpdateError(
                f"netkeep serve still holds the serve lock {_LOCK_WAIT_S:g} s after the login"
                " item stopped. Nothing was changed."
            )
        seams.sleep(_POLL_S)
    try:
        held = seams.claim_lock()
    except Exception as exc:  # a serve started in between
        _restart(item, say, was_on)
        raise UpdateError(
            f"another netkeep took the serve lock ({exc}). Nothing was changed."
        ) from exc
    say("   done: the serve lock is free, and this update holds it")
    return was_on, held


class _Abort(Exception):
    pass


def _restart(item: LoginItem, say: Say, was_on: bool) -> None:
    """Put the login item back the way the update found it, after a step that changed
    nothing failed."""
    if not was_on:
        return
    try:
        item.enable(keep_arguments=True)
        say("   started the login item again")
    except LoginItemError as exc:
        say(f"   could not start the login item again ({exc}); run netkeep login-item on")


def _exec(seams: Seams, flag: str) -> NoReturn:
    """Execute the ``netkeep`` uv just installed with ``update <flag>``."""
    bin_dir = seams.tool_bin()
    executable = None if bin_dir is None else bin_dir / "netkeep"
    if executable is None or not executable.is_file():
        raise UpdateError(
            f"uv installed netkeep, but there is no netkeep in {bin_dir}. Run netkeep update"
            f" {flag} yourself."
        )
    seams.exec_netkeep(executable, ["update", flag])


def _reinstall_old_and_rollback(
    item: LoginItem, seams: Seams, say: Say, state: UpdateState, held: Claim
) -> int:
    """Reinstall the old version with its constraints, holding the serve lock so no
    ``serve`` starts in between, and execute its ``--rollback``."""
    data = item.data
    state = replace(state, phase="rolling-back")
    try:
        write_state(state, data)
        say(f"Rolling back: reinstall netkeep {state.from_version}")
        status = seams.uv(install_command(state.from_version, Path(state.from_constraints)))
    finally:
        held.release()
    if status != 0:
        # The phase stays rolling-back, so the --rollback this asks for still runs.
        write_state(
            replace(state, reason=f"{state.reason} Reinstalling {state.from_version} failed too."),
            data,
        )
        say(f"   failed: uv tool install of {state.from_version} failed (exit status {status}).")
        say("netkeep may not run until you reinstall it. Run this, then netkeep update --rollback:")
        say("   uv " + " ".join(install_command(state.from_version, Path(state.from_constraints))))
        return 1
    _exec(seams, "--rollback")


# -- netkeep update --finish (the new version) -----------------------------------------


def finish(item: LoginItem, seams: Seams, say: Say) -> int:
    """Step 2, on the new version: start the login item and check ``serve`` answers."""
    data = item.data
    try:
        state = read_state(data)
    except UpdateError as exc:
        say(f"error: {exc}")
        return 1
    # "installing" too: the install went through, but recording it didn't.
    if state is None or state.phase not in ("installing", "finishing"):
        say("error: no update is waiting to finish. Run netkeep update.")
        return 1
    if state.to_version != seams.current_version:
        say(
            f"error: this is netkeep {seams.current_version}, but the update installed"
            f" {state.to_version}. Run netkeep update --finish with the new netkeep."
        )
        return 1
    say(f"netkeep update --finish: netkeep {state.to_version} is installed")
    if not state.login_item_was_on:
        clear_state(data)
        say("Done. The login item was off, so netkeep serve isn't started.")
        say("Start it with: netkeep serve")
        return 0
    state = replace(state, new_version_ran=True)
    write_state(state, data)
    failure = _start_and_check(item, seams, say, state.to_version)
    if failure is None:
        clear_state(data)
        say(f"Done: netkeep {state.to_version} is running from the login item.")
        return 0
    say(f"   failed: {failure}")
    state = replace(state, reason=failure)
    held = _stop_quietly(item, seams, say)
    if scheduler_started(data):
        if held is not None:
            held.release()
        return _stop_and_explain(item, say, state, SCHEDULER_RAN)
    if held is None:
        return _stop_and_explain(item, say, state, SERVE_WOULDNT_STOP)
    say(f"netkeep {state.to_version} failed before its scheduler started, so it sent nothing.")
    return _reinstall_old_and_rollback(item, seams, say, state, held)


def _start_and_check(item: LoginItem, seams: Seams, say: Say, version: str) -> str | None:
    """Start the login item and wait for ``serve`` to answer with ``version``. ``None``
    when it does, else what went wrong."""
    say("Start the login item")
    try:
        item.enable(keep_arguments=True)
    except LoginItemError as exc:
        return f"the login item didn't start: {exc}"
    host, port = item.serve_address()
    base = seams.base_url(host, port)
    say(f"Check netkeep serve answers at {base} with version {version}")
    deadline = seams.clock() + SERVE_WAIT_S
    answered: str | None = None
    while True:
        answered = seams.serve_version(base)
        if answered == version:
            say("   done: it answers")
            return None
        if seams.clock() >= deadline:
            break
        seams.sleep(_POLL_S)
    errors = item.data / "logs" / SERVE_ERR_LOG
    if answered is None:
        return f"netkeep serve didn't answer within {SERVE_WAIT_S:g} s (see {errors})"
    return f"a netkeep serve answers, but with version {answered}, not {version} (see {errors})"


def _stop_quietly(item: LoginItem, seams: Seams, say: Say) -> Claim | None:
    """Stop the login item, wait for the serve lock to be free, and take it; the held
    lock, or ``None`` when ``serve`` wouldn't let go."""
    try:
        item.stop()
    except LoginItemError as exc:
        say(f"   could not stop the login item: {exc}")
        return None
    deadline = seams.clock() + _LOCK_WAIT_S
    while seams.lock_state().held:
        if seams.clock() >= deadline:
            say(f"   netkeep serve still holds the serve lock after {_LOCK_WAIT_S:g} s")
            return None
        seams.sleep(_POLL_S)
    try:
        return seams.claim_lock()
    except Exception as exc:  # a serve started in between
        say(f"   another netkeep took the serve lock: {exc}")
        return None


SCHEDULER_RAN: Final = (
    "a serve started its scheduler on this data directory during the update, so it may"
    " have sent messages. Restoring the backup would forget them and could send them twice"
)
SERVE_WOULDNT_STOP: Final = (
    "its serve wouldn't stop, and a backup can't be restored under a running serve"
)


def _stop_and_explain(item: LoginItem, say: Say, state: UpdateState, why: str) -> int:
    """The new version's scheduler ran: no automatic restore, which would forget its sends.
    The login item is turned off, so the version that failed doesn't start again at the
    next login before you've decided."""
    try:
        write_state(replace(state, phase="stopped"), item.data)
    except OSError as exc:
        say(f"   could not record that the update stopped: {exc}")
    try:
        item.disable()
        off = "is off"
    except LoginItemError as exc:
        off = f"could not be turned off ({exc}); run netkeep login-item off"
    say("")
    say(
        f"The update stopped without rolling back: in netkeep {state.to_version}, {why}."
        f" The login item {off}."
    )
    say(f"Why: {state.reason}")
    say("What you can do (docs/guide/troubleshooting.md, Updating):")
    say(
        f" - Look at {item.data / 'logs' / SERVE_ERR_LOG} for why it failed. If it's fixed, run"
        " netkeep login-item on."
    )
    say(
        f" - Or go back to {state.from_version} by hand, knowing that what {state.to_version}"
        " did since the backup is forgotten:"
    )
    say("     uv " + " ".join(install_command(state.from_version, Path(state.from_constraints))))
    if state.backup is not None:
        say(f"     netkeep restore {Path(state.backup).name}")
    say("     netkeep login-item on")
    say(f"Then delete {update_dir(item.data) / STATE_FILE}.")
    return 1


# -- netkeep update --rollback (the old version again) ---------------------------------


def rollback(item: LoginItem, seams: Seams, say: Say) -> int:
    """Step 3, on the reinstalled old version: restore the backup when the new version
    ran on the database, start the login item, and check."""
    data = item.data
    try:
        state = read_state(data)
    except UpdateError as exc:
        say(f"error: {exc}")
        return 1
    if state is None or state.phase != "rolling-back":
        say("error: no rollback is waiting. Run netkeep update.")
        return 1
    if state.from_version != seams.current_version:
        say(
            f"error: this is netkeep {seams.current_version}, but the rollback reinstalled"
            f" {state.from_version}."
        )
        return 1
    if state.restored:
        say(
            "error: this rollback already restored the backup, and netkeep"
            f" {state.from_version} may have run on it since. Restoring it again would forget"
            " that, so netkeep won't."
        )
        say(
            f"Run netkeep login-item on, check netkeep works, then delete"
            f" {update_dir(data) / STATE_FILE}."
        )
        return 1
    say(f"netkeep update --rollback: netkeep {state.from_version} is installed again")
    say(f"Why: {state.reason}")
    if state.new_version_ran:
        if state.backup is None:
            return _stop_and_explain_rollback(
                item, say, state, "there was no database before the update, so no backup"
            )
        backup = Path(state.backup)
        try:
            held = seams.claim_lock()
        except Exception as exc:  # the login item is off; something else serves
            return _stop_and_explain_rollback(item, say, state, f"the serve lock is held: {exc}")
        try:
            # Under the lock, so no serve can start its scheduler after this check.
            if scheduler_started(data):
                return _stop_and_explain(item, say, state, SCHEDULER_RAN)
            # Recorded before the restore and before any serve starts again, so a second
            # --rollback never restores over what the restored version did.
            state = replace(state, restored=True)
            try:
                write_state(state, data)
            except OSError as exc:
                why = f"could not record the restore before making it ({exc})"
                return _stop_and_explain_rollback(item, say, state, why)
            say(f"Restore the backup {backup}, with the login item off")
            try:
                lines = seams.restore(backup)
            except Exception as exc:  # explain whatever stopped the restore
                return _stop_and_explain_rollback(item, say, state, f"the restore failed: {exc}")
        finally:
            held.release()
        for line in lines:
            say(f"   {line}")
    if state.login_item_was_on:
        failure = _start_and_check(item, seams, say, state.from_version)
        if failure is not None:
            return _stop_and_explain_rollback(item, say, state, failure)
    clear_state(data)
    say(f"Rolled back: netkeep {state.from_version} is running again. The update failed:")
    say(f"   {state.reason}")
    return 1


def _stop_and_explain_rollback(item: LoginItem, say: Say, state: UpdateState, why: str) -> int:
    try:
        write_state(
            replace(state, phase="stopped", reason=f"{state.reason} Rollback: {why}."), item.data
        )
    except OSError as exc:
        say(f"   could not record that the rollback stopped: {exc}")
    say(f"   failed: {why}")
    say(
        f"netkeep {state.from_version} is installed again, but the rollback didn't finish."
        " See docs/guide/troubleshooting.md, Updating."
    )
    if state.backup is not None and state.new_version_ran:
        say(f"The backup from before the update is {state.backup}.")
    say(f"When netkeep works again, delete {update_dir(item.data) / STATE_FILE}.")
    return 1


def running_from_tool_install(prefix: Path | None = None, module: Path | None = None) -> bool:
    """Whether this netkeep is a uv tool install: its environment has uv's receipt and
    the package is installed inside it, not linked from a checkout."""
    environment = Path(sys.prefix) if prefix is None else prefix
    package = Path(__file__).resolve().parent.parent if module is None else module
    if not (environment / "uv-receipt.toml").is_file():
        return False
    try:
        package.resolve().relative_to(environment.resolve())
    except ValueError:
        return False
    return True
