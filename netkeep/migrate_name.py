"""The one-time move from netkeeper to netkeep (#661).

The project was called netkeeper. Everything that carried the name on this machine
moves to netkeep the first time the new version opens the data directory (any command
but ``migrate``, ``openapi`` and ``version``), or when you run ``netkeep migrate``.
``netkeep migrate --dry-run`` prints the same plan and changes nothing. Both run
:func:`plan` and :func:`apply`; there is no second copy of the rules.

- **The data directory.** ``~/Library/Application Support/netkeeper`` becomes
  ``.../netkeep`` by one ``os.rename``: one folder, one volume, so it is atomic. Only
  the default location moves; a ``$NETKEEP_DATA`` (or ``$NETKEEPER_DATA``) naming
  anywhere else is left where it is. An old login item's ``NETKEEPER_DATA`` names the
  old default itself, and counts as the default. The move is refused, saying what to
  do, when both folders exist, when a lock in the old one is held (a running
  ``serve``, or a LinkedIn run), when netkeep's Chrome holds the old profile, or when
  ``$NETKEEP_DATABASE_URL`` or ``$NETKEEP_CONFIG`` points inside the old folder.
- **The Keychain.** Each secret under the old service (``netkeeper``, or
  ``netkeeper@<hash>`` for another data directory) is copied to the new one
  (``netkeep``, ``netkeep@<hash>``), through ``keyring``. A secret already under the
  new name is never overwritten, and the old items stay until you remove them.
  ``keyring`` cannot list a service, so the entries are the ones netkeep writes: the
  install's local API key and claim, and each user's Gmail client and mailbox tokens,
  read from the database. No value is printed or logged.
- **The login item.** ``fun.tnkr.netkeeper`` (or ``.<hash>``) is replaced by
  ``fun.tnkr.netkeep`` through the login item's adoption of an old agent
  (:meth:`netkeep.services.login_item.LoginItem.enable`): on stays on, starting at the
  next login, and off stays off. The old plist is removed either way.
- **The lock.** :func:`apply` holds the serve lock of the folder it migrates from its
  last checks through the marker, so no ``serve`` starts on either path meanwhile (the
  lock file moves with the folder), and a second migration is refused.
- **The old agent's own job.** Run by ``fun.tnkr.netkeeper`` itself, it hands over at the
  end: it starts the new agent and stops the old one, whose definition points into the
  moved folder (:meth:`netkeep.services.login_item.LoginItem.replace_old_job`).
- **Only the default.** The automatic run migrates the default data directory only; a
  ``$NETKEEP_DATA`` elsewhere (a scratch folder) waits for ``netkeep migrate``, so it
  reads no Keychain and writes no marker.
- **The marker.** ``migrated-from-netkeeper.json`` in the data directory records what
  was done; with it there, a later start does nothing. Every step is also idempotent
  on its own, so a run that stopped part way finishes the next time.

The database file keeps its name (``netkeeper.sqlite3``, :data:`netkeep.db.DATABASE_FILENAME`).
Tests never reach the real home folder, Keychain or LaunchAgents: ``tests/conftest.py``
sets :data:`path_guard` to refuse them and turns off the automatic run.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import sqlite3
from collections.abc import Callable, Mapping, MutableMapping
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from urllib.parse import unquote

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
from sqlalchemy.engine import make_url

from netkeep import __version__, paths
from netkeep.chrome_profile import profile_lock_pid
from netkeep.db import DATABASE_FILENAME, DATABASE_URL_ENV, make_engine
from netkeep.linkedin import activity_lock
from netkeep.services import keychain, local_api_key, login_item, mailboxes
from netkeep.services.serve_lock import SERVE_LOCK_FILENAME

log = logging.getLogger(__name__)

LEGACY_SERVICE: Final = "netkeeper"
MARKER_FILENAME: Final = "migrated-from-netkeeper.json"
CHROME_PROFILE_DIRNAME: Final = "chrome-profile"
_CHROME_STATE_FILES: Final = ("Local State", "Default/Preferences")
_NO_AUTO: Final = frozenset({"migrate", "openapi", "version"})
"""Commands the automatic run skips: ``migrate`` runs it itself, and the other two read
no data (``make gen-client`` runs ``openapi export`` wherever the repository is)."""

path_guard: Callable[[Path], None] = lambda _path: None  # noqa: E731
"""Called with every folder this module reads or changes. ``tests/conftest.py`` makes it
refuse the real home folder's."""

LoginItemFactory = Callable[[Path], login_item.LoginItem]


class Refused(RuntimeError):
    """The migration would not be safe now; the message says what to do."""


class _DatabaseUnreadable(RuntimeError):
    """The database exists but could not be read, so its users' secrets are unknown."""


@dataclass
class Plan:
    """What :func:`apply` would do, worked out without changing anything."""

    data: Path
    """The data directory once migrated."""
    move_from: Path | None = None
    """The old default data directory, when it moves to :attr:`data`."""
    data_from_env: bool = False
    """``$NETKEEP_DATA`` (or the old name) named the old default; it is pointed at the new."""
    service_from: str | None = None
    service_to: str = ""
    copy: list[str] = field(default_factory=list)
    """Keychain entries under the old service and not yet under the new one."""
    already: list[str] = field(default_factory=list)
    """Entries under both names: left as they are."""
    login: login_item.LegacyAgent | None = None
    login_on: bool = False
    refusals: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    incomplete: bool = False
    """A step could not be planned (the Keychain refused): no marker, so it runs again."""
    keychain_skipped: str | None = None
    """Why there is no Keychain step, when there is none."""
    login_moved: bool = False
    """:func:`apply` replaced or removed the old login item."""
    handed_over: bool = False
    """:func:`apply` started the new login item and stopped the old one, which was this
    process's own job: the caller ends now."""

    @property
    def has_work(self) -> bool:
        return bool(self.move_from or self.copy or self.login)

    def describe(self) -> list[str]:
        """The plan in plain lines, for ``migrate --dry-run``."""
        if self.refusals:
            return [f"refused: {reason}" for reason in self.refusals]
        lines: list[str] = []
        if self.move_from is not None:
            lines.append(f"move the data directory {self.move_from} to {self.data}")
        else:
            lines.append(f"data directory: {self.data} (stays where it is)")
        if self.service_from is None:
            lines.append(f"Keychain: nothing to copy ({self.keychain_skipped or 'no data yet'})")
        elif self.copy:
            lines += [
                f"copy the Keychain item {entry} from service {self.service_from}"
                f" to {self.service_to}"
                for entry in self.copy
            ]
        else:
            lines.append(f"Keychain: nothing under {self.service_from} left to copy")
        lines += [
            f"Keychain: {entry} is already under {self.service_to}; left as it is"
            for entry in self.already
        ]
        if self.login is not None:
            if self.login_on:
                lines.append(
                    f"replace the login item {self.login.label} with"
                    f" {login_item.label_for(self.data)}; it stays on, from the next login"
                )
            else:
                lines.append(
                    f"remove the login item {self.login.label}, which is off; it stays off"
                )
        else:
            lines.append("login item: none from netkeeper")
        lines += self.notes
        return lines


def plan(
    environ: Mapping[str, str] | None = None,
    *,
    macos: bool | None = None,
    login_factory: LoginItemFactory | None = None,
) -> Plan:
    """Work out the migration for this environment. Reads; changes nothing."""
    env = os.environ if environ is None else environ
    legacy = paths.legacy_default_data_dir()
    named = paths.getenv(paths.DATA_ENV, env)
    new_default = paths.default_data_dir()
    chosen = Path(named).expanduser() if named else None
    # A name for either default, however spelled, is the default: the move is checked as
    # for no name (#661). Only another folder is left where it is.
    is_default = chosen is None or _same(chosen, new_default)
    if chosen is not None and legacy is not None and _same(chosen, legacy):
        is_default = True
    if not is_default and chosen is not None:
        result = Plan(data=chosen)
    else:
        named_old = chosen is not None and not _same(chosen, new_default)
        result = Plan(data=new_default, data_from_env=named_old)
        if legacy is not None:
            path_guard(legacy)
            path_guard(new_default)
            if legacy.exists():
                result.move_from = legacy
                _check_move(result, legacy, env)
    if result.refusals:
        return result
    source = result.move_from or result.data
    path_guard(source)
    if not source.is_dir():
        return result  # a fresh install: nothing of netkeeper's to move
    _plan_keychain(result, source, env)
    _plan_login_item(result, env, macos=macos, login_factory=login_factory)
    _plan_notes(result, source, env)
    return result


def _check_move(
    result: Plan, legacy: Path, env: Mapping[str, str], *, holding: str | None = None
) -> None:
    new = result.data
    if holds_database(legacy) and ((new / MARKER_FILENAME).exists() or holds_database(new)):
        result.refusals.append(both_hold_a_database(legacy, new))
        return
    if (new / MARKER_FILENAME).exists():
        result.refusals.append(
            f"{legacy} is back, but your data was already moved to {new}, which netkeep"
            f" uses. {legacy} is not used: check what is in it, move it somewhere else"
            " (don't move the new one), then run netkeep migrate"
        )
        return
    if new.exists():
        result.refusals.append(
            f"both {legacy} and {new} exist, so netkeep can't tell which is yours. Keep the"
            f" one you use: move the other one somewhere else (or rename it), then run"
            " netkeep migrate"
        )
        return
    held = [name for name in _held_locks(legacy / "locks") if name != holding]
    if held:
        result.refusals.append(
            f"netkeeper is still running on {legacy} ({', '.join(held)} held). Stop it:"
            " quit netkeeper serve, or, if the login item runs it, run launchctl bootout"
            " gui/$(id -u)/fun.tnkr.netkeeper (which keeps it on). Then run netkeep migrate"
        )
    pid = profile_lock_pid(legacy / CHROME_PROFILE_DIRNAME)
    if pid is not None and activity_lock.pid_alive(pid):
        result.refusals.append(
            f"netkeeper's Chrome (pid {pid}) is using {legacy / CHROME_PROFILE_DIRNAME}."
            " Quit that Chrome, then run netkeep migrate"
        )
    for name in (DATABASE_URL_ENV, paths.CONFIG_ENV):
        value = paths.getenv(name, env) or ""
        if value and _names_inside(value, legacy):
            result.refusals.append(
                f"${name} points inside {legacy}, which is about to move. Point it at"
                f" {new} instead (the same path with netkeep), then run netkeep migrate"
            )


def _names_inside(value: str, folder: Path) -> bool:
    """Whether a path or a database URL names something in ``folder``: ``~`` expanded,
    percent-decoded, resolved, and compared by path parts, so ``.../netkeeper-backup``
    is not inside ``.../netkeeper``."""
    text_value = value.strip()
    if "://" in text_value:
        try:
            text_value = make_url(text_value).database or ""
        except Exception:  # not a URL after all
            return False
    if not text_value:
        return False
    candidate = Path(unquote(text_value)).expanduser().resolve()
    root = folder.expanduser().resolve()
    return candidate == root or root in candidate.parents


def _held_locks(locks: Path) -> list[str]:
    if not locks.is_dir():
        return []
    held: list[str] = []
    for path in sorted(locks.glob("*.lock")):
        if activity_lock.inspect_at(path, path.stem).held:
            held.append(path.name)
    return held


def _plan_keychain(result: Plan, source: Path, env: Mapping[str, str]) -> None:
    if (paths.getenv(keychain.SERVICE_ENV, env) or "").strip():
        result.keychain_skipped = f"${keychain.SERVICE_ENV} names the service"
        return
    default = paths.is_default_data_dir(result.data)
    digest = "" if default else f"@{paths.data_dir_digest(result.data)}"
    result.service_from = f"{LEGACY_SERVICE}{digest}"
    result.service_to = f"{keychain.DEFAULT_SERVICE}{digest}"
    try:
        entries = _entries(source, env)
    except _DatabaseUnreadable as exc:
        result.incomplete = True
        result.notes.append(
            f"could not read the database ({exc}), so the Gmail secrets are not copied yet;"
            " once it reads again, run netkeep migrate"
        )
        entries = _install_entries()
    try:
        for entry in entries:
            if not keychain.has_secret_under(result.service_from, entry):
                continue
            if not keychain.has_secret_under(result.service_to, entry):
                result.copy.append(entry)
            else:
                result.already.append(entry)
    except keychain.KeychainUnavailable as exc:
        result.incomplete = True
        result.copy.clear()
        result.notes.append(
            f"the Keychain refused ({type(exc).__name__}), so no secret is copied yet; unlock"
            " the login keychain, then run netkeep migrate"
        )


def _entries(source: Path, env: Mapping[str, str]) -> list[str]:
    """The Keychain entries netkeep writes for this data directory (see the module doc)."""
    entries = _install_entries()
    users, tokens = _secret_owners(source, env)
    for user_id in users:
        entries.append(keychain.entry_name(user_id, mailboxes.CLIENT_SECRET_NAME))
    for user_id, ref in tokens:
        entries.append(keychain.entry_name(user_id, ref))
    return list(dict.fromkeys(entries))


def _install_entries() -> list[str]:
    return [
        keychain.install_entry_name(local_api_key.KEY_ENTRY),
        keychain.install_entry_name(local_api_key.CLAIMED_ENTRY),
    ]


_USERS_SQL: Final = "SELECT id FROM users ORDER BY id"
_TOKENS_SQL: Final = (
    "SELECT user_id, keychain_ref FROM mailboxes WHERE keychain_ref != '' ORDER BY id"
)


def _secret_owners(source: Path, env: Mapping[str, str]) -> tuple[list[int], list[tuple[int, str]]]:
    """The user ids, and each mailbox's ``(user_id, keychain_ref)``. Read-only: a SQLite
    database is opened ``mode=ro``, so a dry run writes nothing beside it.

    No database, or one never migrated (no ``users`` or ``mailboxes`` table), has no
    secrets to copy. Any other failure raises :class:`_DatabaseUnreadable`.
    """
    url = paths.getenv(DATABASE_URL_ENV, env)
    try:
        if not url or make_url(url).get_backend_name() == "sqlite":
            database = Path(make_url(url).database or "") if url else source / DATABASE_FILENAME
            if not database.is_file():
                return [], []
            uri = f"{database.resolve().as_uri()}?mode=ro"
            with closing(sqlite3.connect(uri, uri=True)) as connection:
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if not {"users", "mailboxes"} <= tables:
                    return [], []
                users = [int(row[0]) for row in connection.execute(_USERS_SQL)]
                tokens = [(int(u), str(r)) for u, r in connection.execute(_TOKENS_SQL)]
            return users, tokens
        engine = make_engine(url)
        try:
            with engine.connect() as connection:
                inspector = sa_inspect(connection)
                if not (inspector.has_table("users") and inspector.has_table("mailboxes")):
                    return [], []
                users = [int(row[0]) for row in connection.execute(text(_USERS_SQL))]
                tokens = [(int(u), str(r)) for u, r in connection.execute(text(_TOKENS_SQL))]
            return users, tokens
        finally:
            engine.dispose()
    except Exception as exc:
        raise _DatabaseUnreadable(type(exc).__name__) from exc


def _plan_login_item(
    result: Plan,
    env: Mapping[str, str],
    *,
    macos: bool | None,
    login_factory: LoginItemFactory | None,
) -> None:
    is_macos = paths._is_macos() if macos is None else macos
    if not is_macos:
        return
    item = (login_factory or _default_login_item)(result.data)
    old = item.legacy_agent()
    if old is None:
        return
    result.login = old
    result.login_on = item.legacy_enabled(old)


def _default_login_item(data: Path) -> login_item.LoginItem:
    agents = login_item.default_agents_dir()
    path_guard(agents)
    return login_item.LoginItem(data=data, agents_dir=agents)


def _plan_notes(result: Plan, source: Path, env: Mapping[str, str]) -> None:
    """Anything that still names the old folder once it moves: a config file, and the
    Chrome profile's own state (which uses relative paths, but is checked anyway)."""
    if result.move_from is None:
        return
    old = str(result.move_from)
    files: list[Path] = [Path.cwd() / paths.CONFIG_FILENAME, source / paths.CONFIG_FILENAME]
    files += [source / CHROME_PROFILE_DIRNAME / name for name in _CHROME_STATE_FILES]
    for path in dict.fromkeys(files):
        try:
            content = path.read_text(errors="replace")
        except OSError:
            continue
        if old in content:
            shown = (
                (result.data / path.relative_to(source)) if path.is_relative_to(source) else path
            )
            result.notes.append(f"note: {shown} still mentions {old}; change it to {result.data}")


def apply(
    planned: Plan,
    environ: MutableMapping[str, str] | None = None,
    *,
    login_factory: LoginItemFactory | None = None,
    serving: bool = False,
) -> list[str]:
    """Carry out ``planned``. Returns what was done, in plain lines.

    Raises :class:`Refused` when the plan was refused. ``environ`` (``os.environ`` by
    default) has ``$NETKEEP_DATA`` pointed at the new folder when it named the old one.
    """
    env = os.environ if environ is None else environ
    if planned.refusals:
        raise Refused("; ".join(planned.refusals))
    folder = planned.move_from or planned.data
    claim = None
    if folder.is_dir():
        # The serve lock, from the checks through the marker: no serve starts on either
        # path meanwhile (the lock file moves with the folder), and no second migration.
        # Never created with its parents: a folder another migration just moved away
        # must not come back as an empty one holding only a lock file.
        try:
            claim = activity_lock.try_claim_at(
                folder / "locks" / SERVE_LOCK_FILENAME, "serve", create_parents=False
            )
        except FileNotFoundError as exc:
            raise Refused(
                f"{folder} moved while netkeep was migrating it (another migration?);"
                " run netkeep migrate again"
            ) from exc
        if claim is None:
            raise Refused(
                f"a serve or another migration holds {folder / 'locks' / SERVE_LOCK_FILENAME};"
                " stop it, then run netkeep migrate"
            )
    try:
        done = _apply_locked(planned, env, login_factory=login_factory, serving=serving)
    finally:
        if claim is not None:
            claim.release()
    if planned.login_moved:
        handed = _hand_over(planned, login_factory=login_factory)
        if handed is not None:
            done.append(handed)
    return done


def _apply_locked(
    planned: Plan,
    env: MutableMapping[str, str],
    *,
    login_factory: LoginItemFactory | None,
    serving: bool,
) -> list[str]:
    done: list[str] = []
    if planned.move_from is not None:
        recheck = Plan(data=planned.data)
        _check_move(recheck, planned.move_from, env, holding=SERVE_LOCK_FILENAME)
        if recheck.refusals:  # something changed since the plan
            raise Refused("; ".join(recheck.refusals))
        planned.data.parent.mkdir(parents=True, exist_ok=True)
        try:
            planned.move_from.rename(planned.data)  # one rename(2): same folder, same volume
        except OSError as exc:
            reason = errno.errorcode.get(exc.errno or 0, "error")
            hint = (
                " (they are on different volumes: move it yourself with mv, then start netkeep)"
                if exc.errno == errno.EXDEV
                else ""
            )
            raise Refused(
                f"could not rename {planned.move_from} to {planned.data}: {reason}{hint};"
                " nothing was moved. Fix that, then run netkeep migrate"
            ) from exc
        done.append(f"moved your data from {planned.move_from} to {planned.data}")
        log.info("migrate: moved the data directory to its netkeep name")
    if planned.data_from_env:
        _point_env_at(env, planned.data)
    incomplete = planned.incomplete
    try:
        copied = _copy_secrets(planned)
    except keychain.KeychainUnavailable as exc:
        copied, incomplete = 0, True
        done.append(f"the Keychain refused ({exc}); unlock it, then run netkeep migrate again")
    if copied:
        done.append(
            f"copied {copied} Keychain item{'' if copied == 1 else 's'} from service"
            f" {planned.service_from} to {planned.service_to}; the old ones are still there,"
            " and you can delete them in Keychain Access once netkeep works"
        )
    if planned.login is not None:
        try:
            done.append(_move_login_item(planned, login_factory=login_factory, serving=serving))
            planned.login_moved = True
        except login_item.LoginItemError as exc:
            incomplete = True
            done.append(f"could not move the login item ({exc}); run netkeep login-item on")
    done += planned.notes
    if not incomplete and planned.data.is_dir():
        _write_marker(planned, copied)
    return done


def _hand_over(planned: Plan, *, login_factory: LoginItemFactory | None) -> str | None:
    """Under the old login item's own job, start the new one and stop the old (whose
    definition points into the moved folder), now that the lock is released."""
    item = (login_factory or _default_login_item)(planned.data)
    try:
        handed = item.replace_old_job()
    except login_item.LoginItemError as exc:
        return f"could not start the new login item ({exc}); run netkeep login-item on"
    planned.handed_over = handed is not None
    return handed


def _copy_secrets(planned: Plan) -> int:
    if planned.service_from is None:
        return 0
    copied = 0
    for entry in planned.copy:
        if keychain.copy_secret(entry, source=planned.service_from, target=planned.service_to):
            copied += 1
    return copied


def _move_login_item(
    planned: Plan, *, login_factory: LoginItemFactory | None, serving: bool
) -> str:
    item = (login_factory or _default_login_item)(planned.data)
    old = planned.login
    assert old is not None
    if not planned.login_on:
        item.retire_legacy()
        return f"removed the login item {old.label}, which was off; it stays off"
    # Registered, not started: a serve is starting (this one) or none was running.
    item.enable(keep_arguments=True, serving_here=True)
    later = "" if serving else "; run netkeep login-item on to start it now"
    return (
        f"replaced the login item {old.label} with {item.label}: it starts at your next"
        f" login{later}"
    )


def _write_marker(planned: Plan, copied: int) -> None:
    record = {
        "migrated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "version": __version__,
        "moved_from": str(planned.move_from) if planned.move_from else None,
        "data_dir": str(planned.data),
        "keychain_from": planned.service_from,
        "keychain_to": planned.service_to or None,
        "keychain_copied": copied,
        "login_item_from": planned.login.label if planned.login else None,
    }
    marker = planned.data / MARKER_FILENAME
    temporary = marker.with_name(f".{MARKER_FILENAME}.tmp")
    temporary.write_text(json.dumps(record, indent=2) + "\n")
    temporary.replace(marker)


def needed(environ: Mapping[str, str] | None = None) -> bool:
    """Whether a start should run the migration: a cheap check, no Keychain or launchd.

    Only for the default data directory: the old default folder exists, or the new one
    exists without the marker. A ``$NETKEEP_DATA`` naming any other folder (a scratch
    directory, a second install) is left to ``netkeep migrate``: the automatic run reads
    no Keychain and writes no marker for it.
    """
    env = os.environ if environ is None else environ
    legacy = paths.legacy_default_data_dir()
    named = paths.getenv(paths.DATA_ENV, env)
    data = paths.default_data_dir()
    if named:
        chosen = Path(named).expanduser()
        if not (legacy is not None and _same(chosen, legacy)) and not _same(chosen, data):
            return False
    if legacy is not None:
        path_guard(legacy)
        if legacy.exists():
            return True
    path_guard(data)
    return data.is_dir() and not (data / MARKER_FILENAME).exists()


@dataclass(frozen=True, slots=True)
class AutoResult:
    lines: list[str] = field(default_factory=list)
    """What it did, in plain lines; empty when there was nothing to do."""
    handed_over: bool = False
    """It stopped this process's own (old) login item job: the command ends now."""


def auto(command: str | None, *, serving: bool = False) -> AutoResult:
    """The automatic run at a command's start.

    Raises :class:`Refused` when the migration is needed but not safe now.
    """
    if command in _NO_AUTO:
        return AutoResult()
    _follow_moved_folder(os.environ)
    if not needed():
        return AutoResult()
    legacy = paths.legacy_default_data_dir()
    new = paths.default_data_dir()
    if legacy is not None and legacy.exists() and (new / MARKER_FILENAME).exists():
        if holds_database(legacy):
            raise Refused(both_hold_a_database(legacy, new))
        # Already migrated: the data is safe in the new folder, so the old one coming back
        # is a warning, never a refusal (the login item's serve would crash-loop under
        # KeepAlive). `netkeep migrate` says how to clean it up.
        warning = (
            f"warning: {legacy} is back, but your data was moved to {new}, which netkeep"
            " uses; the old folder is ignored. Run netkeep migrate --dry-run to see how to"
            " clean it up"
        )
        log.warning("migrate: %s", warning)
        return AutoResult([warning])
    if (legacy is None or not legacy.exists()) and _serve_lock_held(paths.default_data_dir()):
        # Nothing to move, and a serve runs here: the retry of a step left for later
        # (the Keychain) waits for the next start, without reading anything now.
        return AutoResult()
    planned = plan()
    if planned.refusals:
        raise Refused("; ".join(planned.refusals))
    if not (planned.has_work or planned.data.is_dir()):
        return AutoResult()
    lines = apply(planned, serving=serving)
    return AutoResult(lines, handed_over=planned.handed_over)


def _serve_lock_held(folder: Path) -> bool:
    return activity_lock.inspect_at(folder / "locks" / SERVE_LOCK_FILENAME, "serve").held


def _point_env_at(env: MutableMapping[str, str], data: Path) -> None:
    env[paths.DATA_ENV] = str(data)
    env.pop(paths.legacy_env_name(paths.DATA_ENV), None)


def _follow_moved_folder(env: MutableMapping[str, str]) -> None:
    """Point a ``$NETKEEP_DATA`` that names the old default at the new one, once the old
    one is gone and the new one is there: the old login item's job sets it, and launchd
    restarts that job with its old environment until the next login."""
    legacy = paths.legacy_default_data_dir()
    named = paths.getenv(paths.DATA_ENV, env)
    if legacy is None or not named or not _same(Path(named).expanduser(), legacy):
        return
    path_guard(legacy)
    new = paths.default_data_dir()
    # Moved (the marker), or gone and the new one there: the old name means the new folder,
    # even if something brought an old folder back.
    # Never when the old folder holds a database: that is real data (an undo restored
    # there), and the automatic run refuses rather than pick one.
    moved = (new / MARKER_FILENAME).exists() and not holds_database(legacy)
    if moved or (not legacy.exists() and new.is_dir()):
        _point_env_at(env, new)


def both_hold_a_database(legacy: Path, new: Path) -> str:
    """The one neutral message when both folders may hold your data (an undo restored a
    backup to the old one): it never says which to keep, only when each was last written."""
    return (
        f"both {legacy} ({_last_written(legacy)}) and {new} ({_last_written(new)}) hold a"
        " database; netkeep won't pick one. Move the one you don't want aside, then start"
        " again"
    )


def _last_written(folder: Path) -> str:
    """When ``folder``'s database (or its ``-wal``) was last written, in local time."""
    times = [
        (folder / f"{DATABASE_FILENAME}{suffix}").stat().st_mtime
        for suffix in ("", "-wal", "-shm")
        if (folder / f"{DATABASE_FILENAME}{suffix}").exists()
    ]
    if not times:
        return "no database"
    return "database last written " + datetime.fromtimestamp(max(times)).strftime("%Y-%m-%d %H:%M")


def holds_database(folder: Path) -> bool:
    """Whether ``folder`` has a database file, or its ``-wal`` or ``-shm``."""
    return any(
        (folder / f"{DATABASE_FILENAME}{suffix}").exists() for suffix in ("", "-wal", "-shm")
    )


def _same(left: Path, right: Path) -> bool:
    left, right = left.expanduser().resolve(), right.expanduser().resolve()
    if left == right:
        return True
    try:
        return left.samefile(right)
    except OSError:
        return False
