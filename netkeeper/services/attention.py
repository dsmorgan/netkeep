"""What needs you: the nav counts, the warning flags and the activity state (#508, #347).

One read for the whole app shell, polled once a minute. Each count is the one its page
lists, through that page's own helper, so a badge and its page never disagree:

- Inbox: unhandled replies, unsubscribes and bounces (:func:`inbox.unhandled_count`).
- Dashboard: LinkedIn messages waiting for you and steps waiting for Try again
  (:func:`linkedin_steps.waiting_count`, :func:`linkedin_steps.try_again_count`).
- Triage: contacts waiting for a decision (:func:`triage.progress`'s ``remaining``).
- Imports: draft import runs waiting to be committed or deleted.

A flag is a state that blocks something until you deal with it, as one plain sentence
with no names and no message text: auto-send held (Dashboard), a mailbox that needs you
to sign in again, or else a Gmail draft waiting for a contact who opted out, bounced, is
do not contact or is on the do-not-send list (Gmail, :func:`leftover_drafts.flagged_waiting`,
#543), the session flagged or heat tripped (LinkedIn), and a tripped breaker (Settings;
breakers only, not the full posture check).

The activity state covers LinkedIn runs only (``sync_runs``). A campaign send batch shows
as running beside it, from the ``campaign.tick`` event in the browser (#547), never here;
the Gmail reply poll does not show (it takes seconds and would only blink).
The first match wins: Running, Stopped, Paused, Armed, Idle.

``title_total`` adds up the number badges for the browser tab, without Triage: after an
import its backlog can run to thousands, and the tab would never read clean.

Read-only. A fixed number of statements, whatever the data; ``tests/test_attention.py``
caps it.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy.orm import Session

from netkeeper.config import Settings
from netkeeper.crm import triage
from netkeeper.models import ImportRun, ImportStatus, Mailbox, MailboxStatus, SyncRunKind, User
from netkeeper.scoping import scoped_count
from netkeeper.services import heat, inbox, leftover_drafts, linkedin_steps, route_breaker, runs
from netkeeper.services.linkedin_accounts import (
    LEGACY_ACCOUNT_ID,
    find_account,
    schedule_paused,
    scheduled_runs_armed,
)
from netkeeper.services.linkedin_session import session_flag


class ActivityState(enum.StrEnum):
    """What LinkedIn is doing, in precedence order: the first that applies wins."""

    RUNNING = "running"
    STOPPED = "stopped"
    PAUSED = "paused"
    ARMED = "armed"
    IDLE = "idle"


AUTO_SEND_HELD: Final = "Auto-send is held until you resume it."
AUTO_SEND_HELD_ACTIVITY: Final = (
    "Auto-send is held until you resume it. Other scheduled runs continue."
)
"""The activity sentence when the hold alone blocks: it holds auto-send, not the rest."""
REAUTH_REQUIRED: Final = "A mailbox needs you to sign in again."
DRAFT_FLAGGED: Final = (
    "A Gmail draft waits for a contact you shouldn't email; see Leftover drafts on the Gmail page."
)
SESSION_FLAGGED: Final = "The LinkedIn session is flagged; clear it on the LinkedIn page."
HEAT_TRIPPED: Final = "LinkedIn heat is high, so runs wait until it cools."
BREAKER_TRIPPED: Final = (
    "A breaker is tripped, so some scheduled runs can't start; reset it on the Settings page."
)

PAUSED: Final = "The LinkedIn schedule is paused."
ARMED: Final = "Scheduled LinkedIn runs are armed."
IDLE: Final = "Scheduled LinkedIn runs are off."

_RUN_NAMES: Final = {
    SyncRunKind.CONNECTIONS_FULL: "full connections sync",
    SyncRunKind.CONNECTIONS_INCREMENTAL: "connections sync",
    SyncRunKind.ENRICH: "enrichment",
    SyncRunKind.INBOX: "inbox check",
    SyncRunKind.MESSAGE_SEND: "message",
}


@dataclass(frozen=True, slots=True)
class Counts:
    inbox: int
    dashboard: int
    """``waiting`` and ``try_again`` together."""
    waiting: int
    try_again: int
    triage: int
    imports: int


@dataclass(frozen=True, slots=True)
class Flags:
    """Each flag is a sentence while it is up, ``None`` otherwise."""

    dashboard: str | None
    gmail: str | None
    linkedin: str | None
    settings: str | None


@dataclass(frozen=True, slots=True)
class Activity:
    state: ActivityState
    label: str
    run_id: int | None
    """The running run, while ``state`` is ``running``."""
    run_kind: SyncRunKind | None


@dataclass(frozen=True, slots=True)
class Attention:
    counts: Counts
    flags: Flags
    activity: Activity

    @property
    def title_total(self) -> int:
        """The browser tab's number: every number badge but Triage's."""
        return self.counts.inbox + self.counts.dashboard + self.counts.imports


def breaker_tripped(session: Session, user: User, account_id: int) -> bool:
    """Whether any breaker is tripped: route changed, answer lost, Contact info, or the
    inbox poll's two (messaging page changed, another mailbox's owner). Read-only."""
    return (
        route_breaker.tripped(session, user, account_id)
        or route_breaker.answer_lost_tripped(session, user, account_id)
        or route_breaker.contact_info_tripped(session, user, account_id)
        or route_breaker.inbox_tripped(session, user, account_id)
        or route_breaker.inbox_owner_tripped(session, user, account_id)
    )


def _heat_tripped(
    session: Session, user: User, account_id: int, *, now: datetime, settings: Settings
) -> bool:
    """``posture.heat_status(...).tripped``, without the score and multiplier reads it also
    makes: nothing trips over a non-positive half-life, else :func:`heat.should_skip`."""
    heat_settings = settings.linkedin.heat
    if heat_settings.half_life_hours <= 0:
        return False
    return heat.should_skip(session, user, account_id, now=now, settings=heat_settings)


def _reauth_required(session: Session, user: User) -> bool:
    statement = scoped_count(user, Mailbox).where(Mailbox.status == MailboxStatus.REAUTH_REQUIRED)
    return bool(session.scalar(statement))


def _gmail_flag(session: Session, user: User) -> str | None:
    """A mailbox to sign in to again first: it stops every email step."""
    if _reauth_required(session, user):
        return REAUTH_REQUIRED
    return DRAFT_FLAGGED if leftover_drafts.flagged_waiting(session, user) else None


def _draft_imports(session: Session, user: User) -> int:
    statement = scoped_count(user, ImportRun).where(ImportRun.status == ImportStatus.DRAFT)
    return session.scalar(statement) or 0


def attention(session: Session, user: User, *, now: datetime, settings: Settings) -> Attention:
    """Every count, flag and the activity state for ``user`` at ``now``. Read-only."""
    waiting = linkedin_steps.waiting_count(session, user)
    try_again = linkedin_steps.try_again_count(session, user, now=now)
    counts = Counts(
        inbox=inbox.unhandled_count(session, user),
        dashboard=waiting + try_again,
        waiting=waiting,
        try_again=try_again,
        triage=triage.progress(session, user).remaining,
        imports=_draft_imports(session, user),
    )

    account = find_account(session, user)
    account_id = LEGACY_ACCOUNT_ID if account is None else account.id
    held = linkedin_steps.auto_send_hold(session, user, account_id) is not None
    flagged = session_flag(session, user) is not None
    hot = _heat_tripped(session, user, account_id, now=now, settings=settings)
    breaker = breaker_tripped(session, user, account_id)
    flags = Flags(
        dashboard=AUTO_SEND_HELD if held else None,
        gmail=_gmail_flag(session, user),
        linkedin=SESSION_FLAGGED if flagged else HEAT_TRIPPED if hot else None,
        settings=BREAKER_TRIPPED if breaker else None,
    )

    stopped = (
        flags.linkedin
        or (BREAKER_TRIPPED if breaker else None)
        or (AUTO_SEND_HELD_ACTIVITY if held else None)
    )
    running = None if account is None else runs.running_run(session, user, account.id)
    if running is not None:
        activity = Activity(
            ActivityState.RUNNING,
            f"A LinkedIn {_RUN_NAMES[running.kind]} run is running.",
            running.id,
            running.kind,
        )
    elif stopped is not None:
        activity = Activity(ActivityState.STOPPED, stopped, None, None)
    elif account is not None and schedule_paused(session, user, account.id):
        activity = Activity(ActivityState.PAUSED, PAUSED, None, None)
    elif account is not None and scheduled_runs_armed(session, user, account.id):
        activity = Activity(ActivityState.ARMED, ARMED, None, None)
    else:
        activity = Activity(ActivityState.IDLE, IDLE, None, None)
    return Attention(counts=counts, flags=flags, activity=activity)
