"""One automation switch per service: Off, Manual, Automatic (#829).

Each position maps onto state that already existed; this module reads it as a
position and moves it, and adds nothing that decides a run or a send.

**LinkedIn** (:class:`~netkeep.models.LinkedInAccount`):

- Automatic: armed (``scheduled_runs_armed_at``). ``scheduled_depth``
  says how much the schedule runs: ``quick`` (the incremental sync and the inbox poll)
  or ``full`` (also the full sync and enrichment; the scheduler's gate reads it).
- Manual: disarmed. Runs start only when a person starts them.
- Off: disarmed, with ``automation_off_at`` set. Moving to Off also cancels a run
  already going, as a person's Cancel does; moving to Manual lets it finish.

**Gmail** (the mailboxes' arming, #277): Automatic is armed to send, Manual is armed
for drafts, Off is not armed. One switch moves every mailbox that isn't disconnected
(v1 has at most one). Moving to Automatic needs each one's Message-ID check (#510).

**Doing more needs a person's confirmation.** A move to Automatic, or LinkedIn's depth
from ``quick`` to ``full``, is refused (:class:`ConfirmRequired`) without
``confirm=True``, as arming always was. A move to the left never needs it. Nothing
here touches LinkedIn auto-send: ``[campaigns] linkedin_auto_send`` alone governs it.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy.orm import Session

from netkeep.models import Mailbox, MailboxArm, MailboxStatus, ScheduleDepth, User
from netkeep.scoping import scoped
from netkeep.services import mailboxes as mailbox_service
from netkeep.services import message_id_check, runs
from netkeep.services.linkedin_accounts import (
    arm_scheduled_runs,
    disarm_scheduled_runs,
    ensure_account,
    find_account,
)
from netkeep.services.settings_kv import get_setting, set_setting

log = logging.getLogger(__name__)


class Position(enum.StrEnum):
    """Where a service's switch stands, left to right."""

    OFF = "off"
    MANUAL = "manual"
    AUTOMATIC = "automatic"


#: Left to right: a move to a higher rank does more.
RANK: Final = {Position.OFF: 0, Position.MANUAL: 1, Position.AUTOMATIC: 2}

#: The ``settings_kv`` key that dates the Gmail switch's last move. Display only:
#: the position itself is read from the mailboxes' arming.
GMAIL_CHANGED_KEY: Final = "automation.gmail.changed_at"


class AutomationRefused(RuntimeError):
    """The move is not allowed now; the message says why, in words the UI can show."""


class ConfirmRequired(AutomationRefused):
    """The move does more, and the person has not confirmed it."""


@dataclass(frozen=True, slots=True)
class LinkedInAutomation:
    position: Position
    depth: ScheduleDepth
    changed_at: datetime | None


@dataclass(frozen=True, slots=True)
class GmailAutomation:
    position: Position
    automatic_ready: bool
    """Whether a move to Automatic would be allowed now: a mailbox is connected and
    every connected one has passed its Message-ID check."""
    blocked_reason: str | None
    """Why Automatic isn't allowed now; None when it is (or already is Automatic)."""
    changed_at: datetime | None


# --- LinkedIn --------------------------------------------------------------------


def linkedin_state(session: Session, user: User) -> LinkedInAutomation:
    """The LinkedIn switch's position, depth and last move. Read-only."""
    account = find_account(session, user)
    if account is None:
        # Never armed, never turned off: Manual, with the depth a new account gets.
        return LinkedInAutomation(Position.MANUAL, ScheduleDepth.FULL, None)
    if account.automation_off_at is not None:
        position = Position.OFF
    elif account.scheduled_runs_armed_at is not None:
        position = Position.AUTOMATIC
    else:
        position = Position.MANUAL
    return LinkedInAutomation(position, account.scheduled_depth, account.automation_changed_at)


def set_linkedin(
    session: Session,
    user: User,
    position: Position,
    depth: ScheduleDepth | None = None,
    *,
    confirm: bool,
    now: datetime,
) -> LinkedInAutomation:
    """Move the LinkedIn switch to ``position``, and set ``depth`` when given (else keep
    it). Needs a writer session. Refused without ``confirm`` when it does more."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    before = linkedin_state(session, user)
    new_depth = before.depth if depth is None else depth
    to_automatic = position is Position.AUTOMATIC and before.position is not Position.AUTOMATIC
    to_full = new_depth is ScheduleDepth.FULL and before.depth is ScheduleDepth.QUICK
    if (to_automatic or to_full) and not confirm:
        what = (
            "Automatic lets netkeep visit LinkedIn on its own schedule"
            if to_automatic
            else "Full upkeep adds the weekly full sync and profile refreshes"
        )
        raise ConfirmRequired(f"{what}; confirm to go ahead")
    account = ensure_account(session, user)
    changed = False
    if account.scheduled_depth is not new_depth:
        account.scheduled_depth = new_depth
        changed = True
    if position is Position.AUTOMATIC:
        changed = changed or account.scheduled_runs_armed_at is None
        arm_scheduled_runs(session, user, now=now)  # clears Off too
    else:
        changed = changed or account.scheduled_runs_armed_at is not None
        disarm_scheduled_runs(session, user, now=now)
        off_at = now if position is Position.OFF else None
        if (account.automation_off_at is None) != (off_at is None):
            account.automation_off_at = off_at
            changed = True
        if position is Position.OFF:
            _cancel_running(session, user, account.id, now=now)
    if changed:
        account.automation_changed_at = now
        log.warning(
            "LinkedIn automation for account %d (user %d): %s, %s",
            account.id,
            user.id,
            position.value,
            new_depth.value,
        )
    session.flush()
    return linkedin_state(session, user)


def _cancel_running(session: Session, user: User, account_id: int, *, now: datetime) -> None:
    """Off leaves LinkedIn alone, so a run already going is cancelled the way a person's
    Cancel does it (maintainer, #829): it stops at its next check. Manual lets it finish."""
    running = runs.running_run(session, user, account_id)
    if running is None:
        return
    try:
        runs.request_cancel(session, user, running.id, now=now)
    except runs.RunFinished:
        return  # it ended meanwhile
    log.warning("LinkedIn turned Off: cancel requested for run %d", running.id)


def stop_linkedin(session: Session, user: User, *, now: datetime) -> LinkedInAutomation:
    """What the old stop controls do (``schedule disarm`` and ``pause``, #829): the left
    of the current position and Manual. From Automatic to Manual; Off stays Off, since a
    stop never does more. Needs a writer session."""
    before = linkedin_state(session, user)
    if before.position is Position.OFF:
        return before
    return set_linkedin(session, user, Position.MANUAL, confirm=False, now=now)


# --- Gmail -----------------------------------------------------------------------


def connected_mailboxes(session: Session, user: User) -> list[Mailbox]:
    """Every mailbox the Gmail switch moves: each one not disconnected, oldest first."""
    return list(
        session.scalars(
            scoped(user, Mailbox)
            .where(Mailbox.status != MailboxStatus.DISABLED)
            .order_by(Mailbox.id)
        )
    )


def _position_of(mailbox: Mailbox) -> Position:
    arm = mailbox.arm
    if arm is MailboxArm.SEND:
        return Position.AUTOMATIC
    return Position.OFF if arm is None else Position.MANUAL


#: What the Message-ID check waits for, in the switch's words where arming's differ.
_WAIT_TEXT: Final = message_id_check.WAITING_TEXT


def _blocked_reason(
    session: Session, user: User, mailboxes: list[Mailbox], *, now: datetime
) -> str | None:
    if not mailboxes:
        return "Connect a Gmail mailbox first"
    for mailbox in mailboxes:
        if mailbox.message_id_verified_at is not None:
            continue
        steps = message_id_check.arm_steps(session, user, mailbox, now=now)
        wait = steps.waiting or message_id_check.MessageIdWait.NOT_FOUND
        return (
            f"netkeep hasn't found one of its own drafts on {mailbox.email} by its"
            f" Message-ID yet ({_WAIT_TEXT[wait]})"
        )
    return None


def _changed_at(session: Session, user: User) -> datetime | None:
    raw = get_setting(session, user, GMAIL_CHANGED_KEY)
    if raw is None:
        return None
    try:
        when = datetime.fromisoformat(str(raw))
    except ValueError:
        return None  # display only
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def gmail_state(session: Session, user: User, *, now: datetime) -> GmailAutomation:
    """The Gmail switch's position, whether Automatic is allowed, and its last move.
    Read-only. With several connected mailboxes in different states (not possible in
    v1), the position is the leftmost of them."""
    mailboxes = connected_mailboxes(session, user)
    position = min((_position_of(m) for m in mailboxes), key=RANK.__getitem__, default=Position.OFF)
    blocked = _blocked_reason(session, user, mailboxes, now=now)
    return GmailAutomation(
        position=position,
        automatic_ready=blocked is None,
        blocked_reason=None if position is Position.AUTOMATIC else blocked,
        changed_at=_changed_at(session, user),
    )


def set_gmail(
    session: Session, user: User, position: Position, *, confirm: bool, by: str, now: datetime
) -> GmailAutomation:
    """Move the Gmail switch to ``position`` for every connected mailbox. Needs a writer
    session. Refused without ``confirm`` for a move to Automatic, and refused while any
    connected mailbox lacks its Message-ID check; nothing changes when refused."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    before = gmail_state(session, user, now=now)
    mailboxes = connected_mailboxes(session, user)
    if position is not Position.OFF and not mailboxes:
        raise AutomationRefused("Connect a Gmail mailbox first")
    if position is Position.AUTOMATIC:
        if before.position is not Position.AUTOMATIC and not confirm:
            raise ConfirmRequired(
                "Automatic lets netkeep send approved campaign email on its own;"
                " confirm to go ahead"
            )
        blocked = _blocked_reason(session, user, mailboxes, now=now)
        if blocked is not None:
            raise AutomationRefused(blocked)
    changed = False
    for mailbox in mailboxes:
        if _position_of(mailbox) is position:
            continue
        changed = True
        if position is Position.OFF:
            mailbox_service.disarm(session, user, mailbox)
            continue
        if position is Position.MANUAL or mailbox.arm is None:
            # Drafts first: it arms a mailbox that is off, and takes one back from sending.
            mailbox_service.arm(session, user, mailbox, MailboxArm.DRAFT, by=by, now=now)
        if position is Position.AUTOMATIC:
            mailbox_service.arm(session, user, mailbox, MailboxArm.SEND, by=by, now=now)
    if changed:
        set_setting(session, user, GMAIL_CHANGED_KEY, now.isoformat())
        log.warning("Gmail automation for user %d: %s", user.id, position.value)
    session.flush()
    return gmail_state(session, user, now=now)
