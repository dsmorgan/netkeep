"""The daily update check ``netkeep serve`` makes (#762; ADR 0011 rule 9, ADR 0014).

Every :data:`TICK_S` seconds the job reads the local owner's ``updates.check_daily``
as it stands now (``config.toml``, then the Settings page, then the default, on). While
it is on and the last check is a day old or more (:func:`due`), it asks GitHub for the
newest published release, the same request ``netkeep update --check`` makes
(:func:`netkeep.services.update.latest_release`): one ``GET`` of
:data:`~netkeep.services.update.LATEST_URL`, with no cookie, key or anything about you.
It records the answer and when it asked in ``<data>/update/`` (:data:`CHECK_FILE`), so a
restarted ``serve`` waits out the rest of the day too.

It never installs or changes anything: the UI shows the newer version and the command,
``netkeep update``, and you run it. A failed check logs one line and is recorded like a
successful one, so the next try is a day later, not the next tick. Nothing here raises
into ``serve``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from sqlalchemy.orm import Session, sessionmaker

from netkeep import __version__
from netkeep.config import Settings
from netkeep.db import session_scope
from netkeep.fsutil import write_atomic
from netkeep.logging_setup import personal_exc
from netkeep.services import ui_settings, update
from netkeep.services.users import local_owner

log = logging.getLogger(__name__)

CHECK_FILE: Final = "last-check.json"
"""In the update folder (:func:`~netkeep.services.update.update_dir`): the last check."""

INTERVAL: Final = timedelta(days=1)
"""The least time between two requests."""

TICK_S: Final = 3600.0
"""How often the job looks at whether a check is due: the setting applies within this long."""

FIRST_CHECK_S: Final = 300.0
"""How long after ``serve`` starts the first look waits, so startup is not slowed."""

UPDATE_COMMAND: Final = "netkeep update"
"""What the UI tells you to run. Nothing here runs it."""

REQUEST_TIMEOUT_S: Final = 10.0
"""Each socket operation of the check's request."""
REQUEST_DEADLINE_S: Final = 20.0
"""The whole request. Short, so a Quit while it runs waits little for its thread: the
answer is a small JSON document."""

Fetch = Callable[[str, int], bytes]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def uptime_clock() -> float:
    """Seconds on a clock that keeps counting while the computer sleeps and never jumps
    with the wall clock: ``CLOCK_BOOTTIME`` on Linux, ``CLOCK_MONOTONIC`` on macOS (where,
    unlike :func:`time.monotonic`, it includes sleep)."""
    clock_id = getattr(time, "CLOCK_BOOTTIME", time.CLOCK_MONOTONIC)
    return time.clock_gettime(clock_id)


def github_fetch(url: str, max_bytes: int) -> bytes:
    """The real request: :func:`~netkeep.services.update.https_get`, which reaches only
    GitHub's hosts over HTTPS, with :data:`REQUEST_TIMEOUT_S` and
    :data:`REQUEST_DEADLINE_S`. Tests pass a fake instead."""
    return update.https_get(
        url, max_bytes=max_bytes, timeout_s=REQUEST_TIMEOUT_S, deadline_s=REQUEST_DEADLINE_S
    )


@dataclass(frozen=True, slots=True)
class CheckResult:
    """The last check: when it ran, and the newest release known."""

    checked_at: datetime
    latest: str | None
    """The newest release the last successful check found; kept through a failed one."""
    ok: bool
    """The last check got an answer."""


def check_path(data: Path) -> Path:
    return update.update_dir(data) / CHECK_FILE


def read_result(data: Path, *, warn: bool = True) -> CheckResult | None:
    """The recorded check, or ``None`` when there is none or it can't be read. A record
    that can't be read is logged as a warning when ``warn`` (the job, once a look), else
    at debug level (the API, which reads it on every request)."""
    level = logging.WARNING if warn else logging.DEBUG
    path = check_path(data)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        checked_at = datetime.fromisoformat(raw["checked_at"])
        latest = raw["latest"]
        ok = raw["ok"]
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.log(level, "ignoring the recorded update check: %s", personal_exc(exc))
        return None
    if (
        checked_at.tzinfo is None
        or not isinstance(ok, bool)
        or not (latest is None or (isinstance(latest, str) and update.parse_version(latest)))
    ):
        log.log(level, "ignoring the recorded update check: it isn't one netkeep wrote")
        return None
    return CheckResult(checked_at=checked_at, latest=latest, ok=ok)


def write_result(result: CheckResult, data: Path) -> None:
    path = check_path(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"checked_at": result.checked_at.isoformat(), "latest": result.latest, "ok": result.ok}
    write_atomic(path, json.dumps(body) + "\n")


def due(last: datetime | None, now: datetime) -> bool:
    """A check is due when none ran, or a day or more has passed. A clock set back a
    little waits as usual; a record dated more than a day ahead is taken as wrong, so it
    can't stop the check for good."""
    return last is None or now - last >= INTERVAL or last - now > INTERVAL


def newer_release(result: CheckResult | None, current: str = __version__) -> str | None:
    """The newer release the last check found, or ``None``: none, or not newer than this."""
    if result is None or result.latest is None:
        return None
    latest, running = update.parse_version(result.latest), update.parse_version(current)
    if latest is None or running is None or latest <= running:
        return None
    return result.latest


class DailyUpdateCheck:
    """The background job. :meth:`check_if_due` is one look, and is what tests call."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        base: Settings,
        data: Path,
        *,
        fetch: Fetch | None = None,
        clock: Callable[[], datetime] = _utcnow,
        uptime: Callable[[], float] = uptime_clock,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        tick_s: float = TICK_S,
        first_check_s: float = FIRST_CHECK_S,
    ) -> None:
        self._factory = factory
        self._base = base
        self._data = data
        self._fetch = github_fetch if fetch is None else fetch
        self._clock = clock
        self._uptime = uptime
        self._sleep = sleep
        self._tick_s = tick_s
        self._first_check_s = first_check_s
        self._attempted: float | None = None
        """When this process last asked, on :func:`uptime_clock`."""
        self._written: datetime | None = None
        """The ``checked_at`` this process last recorded, when the write succeeded."""
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run(), name="update-check")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    def check_if_due(self) -> CheckResult | None:
        """Ask GitHub for the newest release when the setting is on and a check is due;
        return what was recorded, or ``None`` when nothing was asked. Never raises for a
        failed request: it logs one line and records the failure."""
        if not self._settings().updates.check_daily:
            return None
        now = self._clock()
        last = read_result(self._data)
        recorded = last.checked_at if last is not None else None
        if not due(recorded, now):
            return None
        # The record decides. Only when it isn't the one this process last wrote (it
        # couldn't be written, or can't be read back) does this process's own memory
        # hold the day, so a broken data directory still means one request a day.
        ours = self._written is not None and recorded == self._written
        if self._attempted is not None and not ours and not self._waited_a_day():
            return None
        self._attempted = self._uptime()
        try:
            latest: str | None = update.latest_release(self._fetch)
            ok = True
        except Exception as exc:  # any failure waits a day, quietly
            log.warning("update check failed; trying again in a day: %s", personal_exc(exc))
            latest, ok = (last.latest if last is not None else None), False
        result = CheckResult(checked_at=now, latest=latest, ok=ok)
        try:
            write_result(result, self._data)
            self._written = now
        except OSError as exc:
            self._written = None
            log.warning("could not record the update check: %s", personal_exc(exc))
        if ok and (found := newer_release(result)) is not None:
            log.info("netkeep %s is available; this is %s", found, __version__)
        return result

    def _waited_a_day(self) -> bool:
        return (
            self._attempted is None or self._uptime() - self._attempted >= INTERVAL.total_seconds()
        )

    def _settings(self) -> Settings:
        """The local owner's settings in force now, or the file's when there is no owner."""
        with session_scope(self._factory) as session:
            user = local_owner(session)
            if user is None:
                return self._base
            return ui_settings.resolve(session, user, self._base)

    async def _run(self) -> None:
        # Off the event loop: the request blocks. A look that fails never ends the job.
        await self._sleep(self._first_check_s)
        while True:
            try:
                await asyncio.to_thread(self.check_if_due)
            except Exception:
                log.exception("update check failed; trying again at the next look")
            await self._sleep(self._tick_s)
