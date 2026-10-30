"""The support bundle (#603): runs, settings and logs in one ``.zip``, at three levels.

You make it (``netkeep support bundle``, or Settings, Support) and send it yourself;
netkeep never uploads it. Every file in it comes from an allowlist: the fields named
here, read through the services that own them, and the log file. Nothing else in the
data directory or the database is read, so a secret (an OAuth client or token, a
cookie, a Keychain value) has no way in; :func:`netkeep.redaction.strip_secrets` runs
over every file at every level all the same.

* **standard** (the default): no personal data. Every file passes through a
  :class:`~netkeep.redaction.Redactor`, and a run's notes (free text you typed) are
  left out.
* **detailed**: the same redaction, longer logs and more runs, and ``redactions.json``,
  how many values each placeholder type stands for. netkeep stores no page-structure
  captures, so the README says there are none.
* **full**: real values, for a hard bug. Still no secrets. The caller confirms first.

Read-only: one plain session, no writes, no browser. Posture is the stored state
(``probe=None``), as the Settings page shows it.
"""

from __future__ import annotations

import enum
import hashlib
import importlib.metadata
import io
import json
import logging
import platform
import plistlib
import re
import zipfile
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from alembic.runtime.migration import MigrationContext
from sqlalchemy.orm import Session

from netkeep import __version__, migrations
from netkeep.config import Settings
from netkeep.logging_setup import LOG_DIRNAME, LOG_FILENAME
from netkeep.models import SyncRun, User
from netkeep.paths import data_dir
from netkeep.redaction import (
    MESSAGE_LEFT_OUT,
    RECORD_START,
    Redactor,
    strip_secrets,
    strip_url_path,
)
from netkeep.scoping import scoped, scoped_count
from netkeep.services import posture as posture_service
from netkeep.services import route_breaker, runs, scheduler, ui_settings
from netkeep.services.linkedin_accounts import (
    account_id_for,
    find_account,
    schedule_pause_state,
)

log = logging.getLogger(__name__)


class Level(enum.StrEnum):
    STANDARD = "standard"
    DETAILED = "detailed"
    FULL = "full"


@dataclass(frozen=True, slots=True)
class LevelSpec:
    """What one level holds, as the Settings page previews it and the README says."""

    level: Level
    title: str
    summary: str
    includes: tuple[str, ...]
    leaves_out: tuple[str, ...]
    default_since_days: int
    max_log_bytes: int
    max_runs: int
    redacted: bool
    needs_confirmation: bool
    warning: str | None = None


_ALWAYS_OUT: Final = (
    "Secrets, at every level: OAuth clients and tokens, cookies, Keychain values, CSRF"
    " tokens. netkeep never reads them for the bundle."
)
_STANDARD_INCLUDES: Final = (
    "Versions: netkeep, Python, macOS, and Chrome when its version can be read"
    " without attaching to it",
    "Settings, with where each value comes from (default, config.toml, Settings page)",
    "Posture, from the stored state (no browser check)",
    "Breakers and the LinkedIn schedule: armed, paused, due times, last fires",
    "Recent runs: kind, status, timings, counts and stop reasons",
    "The database's migration revision",
)
_MB: Final = 1024 * 1024

LEVELS: Final[dict[Level, LevelSpec]] = {
    Level.STANDARD: LevelSpec(
        level=Level.STANDARD,
        title="Standard",
        summary="No personal data. Send it privately to the person helping you.",
        includes=(
            *_STANDARD_INCLUDES,
            "The log file for the chosen days, with names, emails, phone numbers, web"
            " addresses, LinkedIn ids and slugs, and your user name replaced by"
            " placeholders such as <personal-17> or <email-3>",
        ),
        leaves_out=(
            _ALWAYS_OUT,
            "Names, emails, phone numbers, LinkedIn URLs, slugs and URNs, message text and"
            " subject lines, run notes, and exception messages (only the type is kept)",
        ),
        default_since_days=7,
        max_log_bytes=5 * _MB,
        max_runs=50,
        redacted=True,
        needs_confirmation=False,
    ),
    Level.DETAILED: LevelSpec(
        level=Level.DETAILED,
        title="Detailed",
        summary="Still no personal data, with longer logs and more runs.",
        includes=(
            *_STANDARD_INCLUDES,
            "The log file for the chosen days, redacted as at the standard level",
            "How many values each placeholder type stands for (redactions.json)",
            "Page-structure captures from failing runs, when netkeep has any; it stores"
            " none today, and the README says so",
        ),
        leaves_out=(
            _ALWAYS_OUT,
            "Names, emails, phone numbers, LinkedIn URLs, slugs and URNs, message text and"
            " subject lines, run notes, and exception messages (only the type is kept)",
        ),
        default_since_days=30,
        max_log_bytes=25 * _MB,
        max_runs=200,
        redacted=True,
        needs_confirmation=False,
    ),
    Level.FULL: LevelSpec(
        level=Level.FULL,
        title="Full",
        summary="Real names and identifiers, for a hard bug. Send it privately.",
        includes=(
            *_STANDARD_INCLUDES,
            "The log file for the chosen days as it is, with real names, emails, URLs and"
            " LinkedIn ids",
            "Run notes",
        ),
        leaves_out=(_ALWAYS_OUT,),
        default_since_days=30,
        max_log_bytes=25 * _MB,
        max_runs=200,
        redacted=False,
        needs_confirmation=True,
        warning=(
            "This file holds real names, emails and LinkedIn identifiers of the people in"
            " your network. Never attach it to a public issue: send it privately, to"
            " someone you trust."
        ),
    ),
}

MAX_SINCE: Final = timedelta(days=365)
"""The furthest back ``--since`` reaches."""

_SINCE: Final = re.compile(r"^\s*(\d+)\s*([hdw])\s*$", re.IGNORECASE)
_UNITS: Final = {"h": timedelta(hours=1), "d": timedelta(days=1), "w": timedelta(weeks=1)}


class SupportBundleError(ValueError):
    """A bundle that cannot be made as asked: the message says why, in plain words."""


def parse_since(text: str) -> timedelta:
    """``7d``, ``36h`` or ``2w`` as a duration; at least an hour, at most a year."""
    found = _SINCE.match(text)
    if found is None:
        raise SupportBundleError(f"--since takes hours, days or weeks, such as 7d; got {text!r}")
    since = int(found.group(1)) * _UNITS[found.group(2).lower()]
    if since <= timedelta(0) or since > MAX_SINCE:
        raise SupportBundleError("--since must be between 1h and 365d")
    return since


# --- settings: an allowlist ---------------------------------------------------------------

SETTINGS_ALLOWLIST: Final[tuple[str, ...]] = (
    "web.host",
    "web.port",
    "linkedin.cdp_url",
    "linkedin.timezone",
    "linkedin.active_hours",
    "linkedin.weekend_multiplier",
    "linkedin.enrich_stale_days",
    "linkedin.disconnect_after_misses",
    "linkedin.start_chrome_at_startup",
    "linkedin.budget.connection_pages_per_day",
    "linkedin.budget.profile_visits_per_day",
    "linkedin.budget.profile_visits_per_week",
    "linkedin.budget.inbox_polls_per_day",
    "linkedin.budget.li_messages_auto_per_day",
    "linkedin.budget.li_prefills_per_day",
    "linkedin.budget.warmup_start",
    "linkedin.budget.warmup_step",
    "linkedin.pacing.profile_delay_median_s",
    "linkedin.pacing.profile_delay_sigma",
    "linkedin.pacing.distraction_p",
    "linkedin.pacing.distraction_range_s",
    "linkedin.pacing.burst_size",
    "linkedin.pacing.burst_break_s",
    "linkedin.heat.per_block",
    "linkedin.heat.half_life_hours",
    "linkedin.heat.skip_threshold",
    "campaigns.mailbox_daily_cap",
    "campaigns.send_spacing_median_s",
    "campaigns.send_spacing_floor_s",
    "campaigns.contacted_within_days_guard",
    "campaigns.linkedin_auto_send",
    "campaigns.reply_poll_minutes",
    "campaigns.holidays",
    "llm.enabled",
    "llm.model",
    "llm.bulk_model",
    "llm.daily_call_cap",
    "backup.nightly",
    "backup.keep",
    "logs.max_mb",
    "logs.keep",
    "updates.check_daily",
    "menubar.show_icon",
)
"""Every ``Settings`` value the bundle includes. A field not named here never goes in:
``tests/test_support_bundle.py`` fails when ``config.py`` gains one that is in neither
this list nor :data:`SETTINGS_LEFT_OUT`, so each new field is a decision."""

SETTINGS_LEFT_OUT: Final[frozenset[str]] = frozenset()
"""``Settings`` values decided against (none yet: config.toml holds no secret)."""


def _settings_json(settings: Settings) -> dict[str, Any]:
    file_keys = settings.file_keys or frozenset()
    values: dict[str, Any] = {}
    for key in SETTINGS_ALLOWLIST:
        source = (
            "config.toml"
            if key in file_keys
            else "settings page"
            if key in settings.ui_keys
            else "default"
        )
        value = ui_settings.value_at(settings, key)
        if key == "linkedin.cdp_url" and isinstance(value, str):
            value = strip_url_path(value)  # a /devtools/browser/<id> path names one session
        values[key] = {
            "value": ui_settings.to_json(value),
            "source": source,
        }
    return {"config_file": settings.source_path is not None, "values": values}


def settings_leaf_keys(node: object = None, prefix: str = "") -> list[str]:
    """Every dotted ``Settings`` field a config file can set, for the allowlist test."""
    target = Settings() if node is None else node
    keys: list[str] = []
    assert is_dataclass(target)
    for item in fields(target):
        if item.metadata.get("netkeep.config") == "skip":
            continue
        value = getattr(target, item.name)
        key = f"{prefix}{item.name}"
        if is_dataclass(value):
            keys.extend(settings_leaf_keys(value, f"{key}."))
        else:
            keys.append(key)
    return keys


# --- the other sections --------------------------------------------------------------------

CHROME_INFO_PLISTS: Final[tuple[Path, ...]] = (
    Path("/Applications/Google Chrome.app/Contents/Info.plist"),
    Path("~/Applications/Google Chrome.app/Contents/Info.plist"),
)
"""Where Chrome's version is read from on macOS: the app bundle's own file. Reading it
attaches to nothing."""

_PACKAGES: Final = ("fastapi", "sqlalchemy", "alembic", "playwright", "uvicorn", "pydantic")


def chrome_version(plists: Sequence[Path] = CHROME_INFO_PLISTS) -> str | None:
    """Chrome's version from its installed app, or ``None`` when none can be read."""
    for candidate in plists:
        try:
            with candidate.expanduser().open("rb") as handle:
                info = plistlib.load(handle)
        except (OSError, plistlib.InvalidFileException, ValueError):
            continue
        version = info.get("CFBundleShortVersionString") if isinstance(info, dict) else None
        if isinstance(version, str) and version:
            return version
    return None


def _versions(plists: Sequence[Path]) -> dict[str, Any]:
    packages: dict[str, str | None] = {}
    for name in _PACKAGES:
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    mac = platform.mac_ver()[0]
    return {
        "netkeep": __version__,
        "python": platform.python_version(),
        "macos": mac or None,
        "platform": platform.platform(terse=True),
        "machine": platform.machine(),
        "chrome": chrome_version(plists),
        "chrome_note": "read from the installed app; netkeep did not attach to Chrome",
        "packages": packages,
    }


def _database(session: Session) -> dict[str, Any]:
    connection = session.connection()
    return {
        "backend": connection.dialect.name,
        "revision": MigrationContext.configure(connection).get_current_revision(),
        "head": migrations.head_revision(),
    }


def _when(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _breaker(name: str, state: route_breaker.BreakerState) -> dict[str, Any]:
    return {
        "name": name,
        "count": state.count,
        "threshold": state.threshold,
        "tripped": state.tripped,
        "readable": state.readable,
        "since": _when(state.since),
    }


def _breakers(session: Session, user: User, account_id: int) -> list[dict[str, Any]]:
    found = [_breaker("route_changed", route_breaker.state(session, user, account_id))]
    for kind, state in route_breaker.answer_lost_states(session, user, account_id).items():
        found.append(_breaker(f"answer_lost.{kind.value}", state))
    found.append(
        _breaker("contact_info", route_breaker.contact_info_state(session, user, account_id))
    )
    found.append(_breaker("inbox", route_breaker.inbox_state(session, user, account_id)))
    found.append(
        _breaker("inbox_owner", route_breaker.inbox_owner_state(session, user, account_id))
    )
    return found


def _jobs(session: Session, user: User, account_id: int) -> dict[str, Any]:
    account = find_account(session, user)
    pause = schedule_pause_state(session, user, account_id)
    kinds: dict[str, Any] = {}
    for kind in scheduler.JobKind:
        last = scheduler.last_fire(session, user, account_id, kind)
        kinds[kind.value] = {
            "due": _when(scheduler.stored_due(session, user, account_id, kind)),
            "last_fire": None
            if last is None
            else {"at": last.at.isoformat(), "run_id": last.run_id, "reason": last.reason},
        }
    return {
        "armed_at": None if account is None else _when(account.scheduled_runs_armed_at),
        "paused": pause.paused,
        "paused_at": _when(pause.paused_at),
        "kinds": kinds,
    }


_ERROR_TYPE: Final = re.compile(r"^\s*([A-Za-z_]\w*)\s*(:.*)?$", re.DOTALL)


def error_type(error: str | None) -> str | None:
    """A run's ``error`` below the full level: the exception's type and nothing else.

    ``error`` is ``Type: first line of the message``, or the bare type when the message
    is empty (``runs.describe``, which writes ``type(exc).__name__``, never a dotted
    name). The message is whatever the code put in it, a name or a bare slug as often
    as not, with nothing to mark it. An error that does not start with a type keeps
    none of it."""
    if error is None:
        return None
    found = _ERROR_TYPE.match(error)
    if found is None:
        return MESSAGE_LEFT_OUT
    rest = found.group(2)
    return found.group(1) + (f": {MESSAGE_LEFT_OUT}" if rest and rest[1:].strip() else "")


def _run(run: SyncRun, *, redacted: bool) -> dict[str, Any]:
    derived = runs.view(run)
    ended = run.completed_at
    out: dict[str, Any] = {
        "id": run.id,
        "kind": run.kind.value,
        "status": run.status.value,
        "trigger": run.trigger.value,
        "started_at": _when(run.started_at),
        "completed_at": _when(ended),
        "duration_s": None if ended is None else round((ended - run.started_at).total_seconds()),
        "stop_reason": run.stop_reason,
        "stop_reason_text": derived.stop_reason_text,
        "browser_mode": run.browser_mode,
        "max_visits": run.max_visits,
        "resume_of": run.resume_of_id,
        "resumed_by": derived.resumed_by,
        "planned": derived.planned,
        "completed": derived.completed,
        "aging_refused": derived.aging_refused,
        "cancel_requested": run.cancel_requested_at is not None,
        "heartbeat_at": _when(run.heartbeat_at),
        "counts": run.counts_json,
        "progress": run.progress_json,
        "error": error_type(run.error) if redacted else run.error,
    }
    if not redacted:
        out["notes"] = run.notes
    return out


def _runs(
    session: Session, user: User, *, since: datetime, limit: int, redacted: bool
) -> dict[str, Any]:
    total = session.scalar(scoped_count(user, SyncRun).where(SyncRun.started_at >= since)) or 0
    rows = session.scalars(
        scoped(user, SyncRun)
        .where(SyncRun.started_at >= since)
        .order_by(SyncRun.started_at.desc(), SyncRun.id.desc())
        .limit(limit)
    ).all()
    return {
        "since": since.isoformat(),
        "in_window": total,
        "shown": len(rows),
        "runs": [_run(run, redacted=redacted) for run in rows],
    }


def _posture(
    session: Session, user: User, account_id: int, *, now: datetime, settings: Settings
) -> str:
    report = posture_service.posture(
        session, user, account_id, now=now, settings=settings, probe=None
    )
    return posture_service.render(report)


# --- the log file -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LogExcerpt:
    parts: tuple[str, ...]
    """The records, in order, each a whole record."""
    files: tuple[str, ...]
    records: int
    truncated: bool


def log_files(directory: Path) -> list[Path]:
    """``netkeep.log`` and its rotated files, oldest first."""
    rotated: list[tuple[int, Path]] = []
    for candidate in directory.glob(f"{LOG_FILENAME}.*"):
        suffix = candidate.name.removeprefix(f"{LOG_FILENAME}.")
        if suffix.isdigit():
            rotated.append((int(suffix), candidate))
    found = [path for _, path in sorted(rotated, reverse=True)]
    current = directory / LOG_FILENAME
    if current.is_file():
        found.append(current)
    return found


def log_excerpt(directory: Path, *, since_local: datetime, max_bytes: int) -> LogExcerpt:
    """The records written since ``since_local`` (local time, as the file has it).

    Whole records only: a line that does not start with a date continues the record
    above it, and stays or goes with it, so a personal span is never cut in two. Lines
    before a file's first record (another file's tail) are dropped. Past ``max_bytes``,
    the oldest records go first, as the files are read, so memory stays near
    ``max_bytes`` however long the log is.
    """
    kept: deque[tuple[str, int]] = deque()
    size = 0
    truncated = False
    used: list[str] = []
    cutoff = since_local.strftime("%Y-%m-%d %H:%M:%S")

    def keep(record: list[str]) -> None:
        nonlocal size, truncated
        text = "".join(record)
        length = len(text.encode())
        kept.append((text, length))
        size += length
        while size > max_bytes and kept:
            size -= kept.popleft()[1]
            truncated = True

    for path in log_files(directory):
        current: list[str] | None = None
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    started = RECORD_START.match(line)
                    if started is not None:
                        if current is not None:
                            keep(current)
                        current = [line] if started.group(1) >= cutoff else None
                    elif current is not None:
                        current.append(line)
        except OSError as exc:
            log.warning("support bundle: cannot read a log file: %s", type(exc).__name__)
            continue
        used.append(path.name)
        if current is not None:
            keep(current)
    return LogExcerpt(
        parts=tuple(text for text, _ in kept),
        files=tuple(used),
        records=len(kept),
        truncated=truncated,
    )


# --- the bundle ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Bundle:
    data: bytes
    filename: str
    level: Level
    files: tuple[str, ...]


_LOG_CHUNK_RECORDS: Final = 2000


@dataclass(slots=True)
class _Files:
    redactor: Redactor | None
    written: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    def add(self, name: str, text: str) -> None:
        cleaned = strip_secrets(text) if self.redactor is None else self.redactor.text(text)
        self.written[name] = cleaned

    def add_log(self, name: str, records: Sequence[str]) -> None:
        """The log, redacted a chunk of whole records at a time: the copies redaction
        makes stay the size of a chunk, not of the log."""
        chunks: list[str] = []
        for start in range(0, len(records), _LOG_CHUNK_RECORDS):
            text = "".join(records[start : start + _LOG_CHUNK_RECORDS])
            chunks.append(strip_secrets(text) if self.redactor is None else self.redactor.log(text))
        self.written[name] = "".join(chunks)

    def add_json(self, name: str, value: object) -> None:
        self.add(name, json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n")

    def section(self, name: str, build: Callable[[], None]) -> None:
        """Build one section; one that fails is named in the README, never fatal. Only
        the exception's type is kept: its message could carry a personal value."""
        try:
            build()
        except Exception as exc:
            log.warning("support bundle: %s failed: %s", name, type(exc).__name__)
            self.problems.append(f"{name}: {type(exc).__name__}")


_DESCRIPTIONS: Final = {
    "versions.json": "netkeep, Python, macOS and Chrome versions",
    "settings.json": "every setting the bundle allows, its value and where it comes from",
    "posture.txt": "the protections report, from stored state (no browser check)",
    "breakers.json": "the streak breakers: counts, thresholds, tripped or not",
    "jobs.json": "the LinkedIn schedule: armed, paused, each kind's due time and last fire",
    "runs.json": "recent runs: kind, status, timings, counts, stop reasons",
    "database.json": "the database backend and migration revision",
    "logs/netkeep.log": "the log file's records for the chosen days",
    "redactions.json": "how many values each placeholder type stands for",
}


def build_bundle(
    session: Session,
    user: User,
    *,
    settings: Settings,
    level: Level = Level.STANDARD,
    since: timedelta | None = None,
    now: datetime | None = None,
    log_directory: Path | None = None,
    chrome_plists: Sequence[Path] = CHROME_INFO_PLISTS,
) -> Bundle:
    """The bundle for ``user`` at ``level``, as zip bytes. Reads only.

    ``since`` defaults to the level's own (7 days standard, 30 otherwise). The full
    level's confirmation is the caller's: this function makes what it is asked for.
    """
    spec = LEVELS[level]
    moment = datetime.now(UTC) if now is None else now
    window = timedelta(days=spec.default_since_days) if since is None else since
    start = moment - window
    home = Path.home()
    redactor = Redactor([(data_dir(), "<data>"), (home, "~")]) if spec.redacted else None
    out = _Files(redactor)
    account_id = account_id_for(session, user)

    out.section("versions", lambda: out.add_json("versions.json", _versions(chrome_plists)))
    out.section("settings", lambda: out.add_json("settings.json", _settings_json(settings)))
    out.section(
        "posture",
        lambda: out.add(
            "posture.txt", _posture(session, user, account_id, now=moment, settings=settings)
        ),
    )
    out.section(
        "breakers", lambda: out.add_json("breakers.json", _breakers(session, user, account_id))
    )
    out.section("jobs", lambda: out.add_json("jobs.json", _jobs(session, user, account_id)))
    out.section(
        "runs",
        lambda: out.add_json(
            "runs.json",
            _runs(session, user, since=start, limit=spec.max_runs, redacted=spec.redacted),
        ),
    )
    out.section("database", lambda: out.add_json("database.json", _database(session)))

    directory = data_dir() / LOG_DIRNAME if log_directory is None else log_directory
    excerpt = log_excerpt(
        directory,
        since_local=start.astimezone().replace(tzinfo=None),
        max_bytes=spec.max_log_bytes,
    )
    if excerpt.files:
        out.add_log("logs/netkeep.log", excerpt.parts)
    if redactor is not None and level is Level.DETAILED:
        out.add_json(
            "redactions.json",
            {
                "note": "Each placeholder stands for one value; the same value has the"
                " same placeholder in every file of this bundle.",
                "types": redactor.counts(),
            },
        )

    created = moment.astimezone(UTC)
    readme = _readme(spec, created=created, window=window, excerpt=excerpt, out=out)
    out.add("README.txt", readme)
    manifest = {
        "format": 1,
        "level": level.value,
        "created_at": created.isoformat(),
        "netkeep_version": __version__,
        "since": start.isoformat(),
        "redacted": spec.redacted,
        "log": {
            "files": list(excerpt.files),
            "records": excerpt.records,
            "truncated": excerpt.truncated,
        },
        "problems": list(out.problems),
        "files": [
            {
                "name": name,
                "bytes": len(text.encode()),
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
            }
            for name, text in sorted(out.written.items())
        ],
    }
    out.add_json("manifest.json", manifest)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in ("README.txt", "manifest.json"):
            archive.writestr(name, out.written[name])
        for name, text in sorted(out.written.items()):
            if name not in ("README.txt", "manifest.json"):
                archive.writestr(name, text)
    stamp = created.strftime("%Y%m%d-%H%M%S")
    return Bundle(
        data=buffer.getvalue(),
        filename=f"netkeep-support-{level.value}-{stamp}.zip",
        level=level,
        files=tuple(sorted(out.written)),
    )


def _readme(
    spec: LevelSpec,
    *,
    created: datetime,
    window: timedelta,
    excerpt: LogExcerpt,
    out: _Files,
) -> str:
    days = window.total_seconds() / 86400
    span = f"{days:g} days" if days >= 1 else f"{window.total_seconds() / 3600:g} hours"
    lines = [
        f"netkeep support bundle, {spec.title.lower()} level",
        f"Made {created:%Y-%m-%d %H:%M} UTC by netkeep {__version__}, covering the last {span}.",
        "",
        spec.summary,
        "",
    ]
    if spec.warning is not None:
        lines += [f"WARNING: {spec.warning}", ""]
    lines += ["What is inside:"]
    for name in sorted(out.written):
        lines.append(f"  {name}: {_DESCRIPTIONS.get(name, '')}".rstrip(": ").rstrip())
    lines += ["  manifest.json: every file, its size and checksum, the level, and when", ""]
    if not excerpt.files:
        lines += ["There is no log file yet, so the bundle has none.", ""]
    elif excerpt.truncated:
        lines += ["The log was longer than this level keeps; the oldest records were left out.", ""]
    if spec.level is not Level.STANDARD:
        lines += [
            "Page-structure captures: netkeep does not store any today, so this bundle"
            " has none. If a maintainer needs one, they will say how to make it.",
            "",
        ]
    if spec.redacted:
        lines += [
            "Personal values are replaced by placeholders such as <personal-17>, <email-3>,"
            " <url-5>, <urn-2>, <slug-4>, <phone-1>. The same value has the same"
            " placeholder in every file, and no placeholder can be turned back into its"
            " value. <data> is netkeep's data directory, ~ your home directory.",
            "",
        ]
    lines += ["Left out:"] + [f"  {item}" for item in spec.leaves_out] + [""]
    if out.problems:
        lines += ["Sections that could not be read (the error type only):"]
        lines += [f"  {problem}" for problem in out.problems] + [""]
    if spec.redacted:
        lines += [
            "Known gap: a name or other value the code logged without marking it, inside a"
            " log message, can still appear in logs/netkeep.log when no pattern"
            " recognizes it. Skim that file before you share the bundle.",
            "",
        ]
    lines += [
        "How to send it: netkeep never uploads this file. "
        + (
            "Send it privately to the person helping you; never attach it to a public issue."
            if not spec.redacted
            else "Send it privately (by email) to the person helping you, and say in your"
            " GitHub issue that you have one. Attach it to a public issue only after you"
            " have skimmed it."
        ),
        "",
    ]
    return "\n".join(lines)
