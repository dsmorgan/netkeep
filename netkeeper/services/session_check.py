"""Check connection's bookkeeping (#652): its run row, its refusals, and what it read.

``POST /linkedin/browser/check`` records a ``session_check`` run and submits it to the
task runner; :meth:`netkeeper.worker.BrowserWorker.execute` runs it. This module holds
what the run writes, and nothing here touches a browser:

1. **Before the lock** (:func:`start`): the run is refused while another netkeeper
   process holds the account's browser lock (:class:`BrowserInUse`), and
   :func:`~netkeeper.services.runs.create_run` refuses it while any run of the account is
   still ``running``, so one check, or one browser run of any kind, runs at a time. Then
   it records a manual ``session_check`` run through
   :data:`~netkeeper.services.runs.SESSION_CHECK_GATE`.
2. **Under the lock** (the worker): the same attach every run makes, with the
   dedicated-profile check (ADR 0012 rule 6, #624), then
   :func:`netkeeper.linkedin.preflight.read_session` on the run's own blank tab: a few
   ``navigator`` properties and one ``Network.getCookies`` scoped to LinkedIn's URL
   (#633). The tab closes and the connection detaches.
3. **After** (:func:`finish`): the run ends ``completed``, stop reason
   :data:`SESSION_CHECK_STOP`, with the posture report's ``linkedin session`` row in
   ``counts_json``. A definite answer (logged in, or no session) is recorded as session
   evidence from ``check run <id>``, which ``GET /linkedin/browser/health`` reads. A
   Chrome that is unreachable, busy, or not netkeeper's profile ends the run ``failed``
   with the worker's usual stop reasons, and health reads ``browser_unavailable`` too.

**What it doesn't gate on, and why.** The check navigates nowhere, so no request
reaches linkedin.com: it spends no budget unit (profile visits or any other), and the
session flag, heat, and ``[linkedin] active_hours`` don't stop it, the same as
``netkeeper preflight``, which is how a person sees they are signed in again. It never
clears the session flag: that stays a person's confirmed act (``POST
/linkedin/session-flag/clear``). Cookie names cross into the run and the evidence,
never a value.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy.orm import Session, sessionmaker

from netkeeper.db import session_scope
from netkeeper.linkedin import activity_lock
from netkeeper.models import JsonValue, SyncRun, SyncRunKind, SyncRunStatus, SyncRunTrigger, User
from netkeeper.services import runs
from netkeeper.services.linkedin_accounts import ensure_account
from netkeeper.services.linkedin_session import record_session_evidence
from netkeeper.services.posture import SessionProbe, probe_row

log = logging.getLogger(__name__)

#: The stop reason of a check that read the session, whatever it found.
SESSION_CHECK_STOP: Final = "session_check"
#: The note on every check's run.
SESSION_CHECK_NOTE: Final = (
    "Check connection (#652): read the LinkedIn session from Chrome's cookie jar on a"
    " blank tab; no page was visited and no budget was spent"
)


class BrowserInUse(runs.RunError):
    """Another netkeeper process holds the account's browser lock; the message says which."""


@dataclass(frozen=True, slots=True)
class CheckResult:
    """What the check read: the probe the posture row is built from, and preflight's
    other warnings (the fingerprint, an unreadable jar). Names and fixed words only."""

    probe: SessionProbe
    warnings: tuple[str, ...] = ()


def start(session: Session, user: User, *, now: datetime) -> SyncRun:
    """Step 1 of the module docstring. Raises :class:`BrowserInUse`, and what
    :func:`runs.create_run` raises (:class:`runs.RunAlreadyRunning` while a run is
    running). Needs a writer session."""
    account_id = ensure_account(session, user).id
    # A run of this database shows as running and create_run names it; a lock held with
    # no running row is another process (`netkeeper preflight`, a CLI run) attached now.
    held = runs.browser_held_for(session)(account_id)
    busy = runs.running_run(session, user, account_id)
    if held and busy is None:
        holder = activity_lock.read_holder(activity_lock.account_key(account_id))
        raise BrowserInUse(
            f"{'another netkeeper process' if holder is None else holder.describe()} holds"
            " the browser for this account; try again when it has finished"
        )
    run = runs.create_run(
        session,
        user,
        SyncRunKind.SESSION_CHECK,
        trigger=SyncRunTrigger.MANUAL,
        now=now,
        gate=runs.SESSION_CHECK_GATE,
    )
    run.notes = SESSION_CHECK_NOTE
    return run


def counts(result: CheckResult) -> dict[str, JsonValue]:
    """The run's ``counts_json``: the ``linkedin session`` row, word for word as
    ``GET /linkedin/browser/health`` names its fields, and the other warnings."""
    row = probe_row(result.probe)
    return {
        "session_status": row.status.value,
        "session_summary": row.value,
        "session_warnings": list(row.warnings),
        "warnings": list(result.warnings),
        "cookie_names": list(result.probe.cookie_names),
    }


def finish(
    factory: sessionmaker[Session], user_id: int, run_id: int, result: CheckResult, *, now: datetime
) -> None:
    """Step 3: the run's ending and, for a definite answer, the session evidence."""
    with session_scope(factory, write=True) as session:
        user = session.get(User, user_id)
        if user is None:
            return
        logged_in = result.probe.logged_in
        if result.probe.attached and logged_in is not None:
            record_session_evidence(
                session,
                user,
                logged_in=logged_in,
                source=f"check run {run_id}",
                cookie_names=result.probe.cookie_names,
                now=now,
            )
        runs.finish_run(
            session,
            user,
            run_id,
            status=SyncRunStatus.COMPLETED,
            now=now,
            stop_reason=SESSION_CHECK_STOP,
            counts=counts(result),
        )
