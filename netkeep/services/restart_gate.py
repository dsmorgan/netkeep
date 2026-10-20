"""When **Restart netkeep** may stop ``serve``: never in the middle of work (#597).

A restart stops ``serve`` the way Ctrl-C does, and that cancels what is running: a
LinkedIn run (a prefill or an auto-send can be mid-typing), a background task, a
campaign tick that may be mid-send, a scheduler heartbeat between claiming a fire and
recording it. So :func:`stored_busy` and :func:`live_busy` name anything in flight,
and the endpoint refuses (``409``) while there is any, or, when asked to restart when
idle, :class:`RestartGate` waits for it:

1. every :data:`POLL_S`, it asks them until nothing is running;
2. then it quiesces: the scheduler and the nightly backup pause, and the campaign
   engine stops and waits for a tick in flight, so no new scheduled work can start;
3. it asks again, waiting on anything started in between (a run a person started by
   hand, which then keeps scheduled runs and sends paused until it ends), and only
   then triggers the restart.

:meth:`RestartGate.cancel` (**Cancel** on the banner) stops the wait and resumes the
scheduler and the engine if it had paused them. An error while it looks never ends the
wait: it counts as busy, and it looks again (#707).

This module never imports :mod:`netkeep.services.restart`, the one module that
execs: ``serve`` puts its restarter on ``app.state`` as a :class:`RestartHook`, and
``netkeep/web`` reaches it only through this protocol.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Final, Literal, Protocol, runtime_checkable

from sqlalchemy.orm import Session, sessionmaker

from netkeep.db import session_scope
from netkeep.linkedin import activity_lock
from netkeep.logging_setup import personal_exc
from netkeep.models import SyncRun, SyncRunStatus
from netkeep.models.base import utcnow
from netkeep.scoping import scoped
from netkeep.services import runs
from netkeep.services.linkedin_accounts import local_account_id
from netkeep.services.tasks import TaskRunner
from netkeep.services.users import local_users

log = logging.getLogger(__name__)

POLL_S: Final = 2.0
"""How often a restart waiting for idle looks again."""

CHECK_FAILED: Final = "netkeep could not check what is running (see the log); it looks again"
"""What a restart waits on while its busy check or the quiesce raises (#707)."""

ERROR_LOG_S: Final = 60.0
"""While errors repeat, a restart waiting for idle logs at most one line this often,
after each kind of error's one traceback (#707 review)."""


@runtime_checkable
class RestartHook(Protocol):
    """What ``serve`` puts on ``app.state.restarter``."""

    @property
    def mode(self) -> Literal["launchd", "exec"]: ...

    @property
    def requested(self) -> bool: ...

    def trigger(self) -> None: ...


def stored_busy(factory: sessionmaker[Session]) -> list[str]:
    """What the database and the lock files say is running: a run row still
    ``running`` for a local user, or the local account's browser lock held. A row a
    crashed process left ``running`` (``runs.is_stale``) does not count, so it can
    never hold a restart off for good. A lock that cannot be read (``OSError``)
    counts as busy."""
    reasons: list[str] = []
    now = utcnow()
    with session_scope(factory) as session:
        users = local_users(session)
        for user in users:
            running = session.scalars(
                scoped(user, SyncRun).where(SyncRun.status == SyncRunStatus.RUNNING)
            ).all()
            for run in running:
                if runs.is_stale(session, run, now=now):
                    continue  # left by a process that went away: nothing to cut off
                reasons.append(
                    f"a LinkedIn {run.kind.value.replace('_', ' ')} run (#{run.id}) is going;"
                    " cancel it or wait for it to finish"
                )
        account = local_account_id(session)
    key = (
        activity_lock.SINGLE_ACCOUNT_KEY if account is None else activity_lock.account_key(account)
    )
    try:
        state = activity_lock.inspect(key)
    except OSError as exc:
        # Unreadable is not idle (#707): the browser may be in use.
        log.warning("restart: could not read the browser lock: %s", personal_exc(exc))
        reasons.append("netkeep could not tell whether the browser is in use; it looks again")
        return reasons
    if state.held:
        who = state.holder.describe() if state.holder is not None else "another process"
        reasons.append(f"the browser is in use by {who}")
    return reasons


class Busy(Protocol):
    @property
    def busy(self) -> bool: ...


def live_busy(
    tasks: TaskRunner | None,
    engine: Busy | None,
    backups: Busy | None = None,
    scheduler: Busy | None = None,
) -> list[str]:
    """What this process has in flight: a background task, a campaign tick, the nightly
    backup (an exec would kill its ``VACUUM INTO`` part way), or a scheduler heartbeat
    (#707: between claiming a fire and submitting its run, neither the task runner nor
    the run rows show it)."""
    reasons = [
        f"a background task ({info.name}) is running"
        for info in ([] if tasks is None else tasks.active())
    ]
    if scheduler is not None and scheduler.busy:
        reasons.append("the scheduler is starting or finishing a scheduled run")
    if engine is not None and engine.busy:
        reasons.append("a campaign send is in progress")
    if backups is not None and backups.busy:
        reasons.append("the nightly backup is being written")
    return reasons


class CancelInProgress(RuntimeError):
    """A restart was asked for while a Cancel was still resuming what it paused."""


class RestartGate:
    """A restart that waits for idle, at most one per process (see the module)."""

    def __init__(
        self,
        hook: RestartHook,
        *,
        busy: Callable[[], Awaitable[list[str]]],
        quiesce: Callable[[], Awaitable[None]],
        resume: Callable[[], None],
        poll_s: float = POLL_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._hook = hook
        self._busy = busy
        self._quiesce = quiesce
        self._resume = resume
        self._quiesced = False
        self._quiescing: asyncio.Task[None] | None = None
        self._cancelling = False
        self._poll_s = poll_s
        self._monotonic = monotonic
        self._task: asyncio.Task[None] | None = None
        self.waiting_on: list[str] = []

    @property
    def waiting(self) -> bool:
        """A restart was asked for and has not stopped serve yet."""
        return self._task is not None and not self._task.done()

    @property
    def cancelling(self) -> bool:
        """A Cancel is undoing the quiesce; nothing may start until it has."""
        return self._cancelling

    def start(self) -> None:
        """Begin waiting for idle, then restart. Idempotent while it waits.
        :class:`CancelInProgress` while a Cancel is still resuming what it paused."""
        if self._cancelling:
            raise CancelInProgress("a cancel is in progress; try again in a moment")
        if not self.waiting and not self._hook.requested:
            self._task = asyncio.get_running_loop().create_task(self._run(), name="restart-gate")

    async def cancel(self) -> bool:
        """Stop waiting, and undo the quiesce if it happened: scheduled runs fire and
        campaign sends go out again. False when there was nothing to cancel (no wait,
        or the restart already began)."""
        task = self._task
        if task is None or task.done() or self._hook.requested or self._cancelling:
            return False
        # Until everything is resumed, a second restart is refused (start), so it can
        # never stop serve while this resumes the engine in it.
        self._cancelling = True
        try:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self.waiting_on = []
            quiescing = self._quiescing
            if quiescing is not None and not quiescing.done():
                # Never cut the quiesce off part way (the engine may be waiting out a
                # send): let it finish, then undo all of it.
                with contextlib.suppress(Exception):
                    await quiescing
            if self._quiesced:
                self._resume()
                self._quiesced = False
        finally:
            self._task = None
            self._cancelling = False
        log.info("the restart waiting for idle was cancelled")
        return True

    async def _run(self) -> None:
        quiet = False  # the quiesce finished without an error
        logged: list[str] | None = None  # the reasons a clean look last logged
        traced: set[type[Exception]] = set()  # the errors whose traceback is logged
        failures = 0  # looks that raised since the last traceback or warning
        warned_at = 0.0
        while True:
            try:
                reasons = await self._busy()
                self.waiting_on = reasons
                if not reasons:
                    if quiet:
                        break
                    # Marked before the wait, so a Cancel during it still resumes what was
                    # paused; shielded, so that Cancel does not cut the quiesce off part way.
                    self._quiesced = True
                    self._quiescing = asyncio.ensure_future(self._quiesce())
                    await asyncio.shield(self._quiescing)
                    quiet = True
                    continue  # look once more: something may have started meanwhile
                if reasons != logged:
                    log.info("restart waits: %s", "; ".join(reasons))
                    logged = reasons
            except Exception as exc:
                # Never stop waiting on an error (#707): this task would end with the
                # scheduler, the engine and backups paused, and Cancel could not undo it.
                # Unknown is not idle, and a quiesce that failed is tried again. Each kind
                # of error logs its traceback once while the gate waits; after that, one
                # line at most every ERROR_LOG_S, so an error that persists or comes and
                # goes never rotates the rest of the log away.
                now = self._monotonic()
                if type(exc) not in traced:
                    traced.add(type(exc))
                    log.exception("restart: could not check what is running; trying again")
                    failures, warned_at = 0, now
                else:
                    failures += 1
                    if now - warned_at >= ERROR_LOG_S:
                        log.warning(
                            "restart: still could not check what is running (%s; %d failed"
                            " looks since the last report); trying again",
                            type(exc).__name__,
                            failures,
                        )
                        failures, warned_at = 0, now
                self.waiting_on = [CHECK_FAILED]
            await asyncio.sleep(self._poll_s)
        self._hook.trigger()
