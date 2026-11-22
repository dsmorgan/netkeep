"""A user's LinkedIn account row (spec 8.4, ADR 0005).

Budgets, heat, and the scheduler's state are keyed by the id this returns. v1
has one account per user, labelled ``default``; migration 0011 created it for
every user that existed then, and :func:`ensure_account` creates it for any user
made since, on the first run that needs it.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from netkeep.db import is_writer
from netkeep.models import DEFAULT_ACCOUNT_LABEL, LinkedInAccount, ScheduleDepth, User
from netkeep.scoping import scoped
from netkeep.services.users import local_owner

log = logging.getLogger(__name__)

#: The id every caller used before ``linkedin_accounts`` existed, and still the id
#: the first user's account gets (migration 0011 inserts in user id order into an
#: empty table). Read-only callers fall back to it for a user with no row yet:
#: nothing can have been spent under any other id for that user.
LEGACY_ACCOUNT_ID = 1


def find_account(session: Session, user: User) -> LinkedInAccount | None:
    """``user``'s default account, or ``None`` when it has not been created yet. Read-only."""
    statement = scoped(user, LinkedInAccount).where(LinkedInAccount.label == DEFAULT_ACCOUNT_LABEL)
    return session.scalars(statement).one_or_none()


def ensure_account(session: Session, user: User) -> LinkedInAccount:
    """``user``'s default account, created on first use. Needs a writer session."""
    existing = find_account(session, user)
    if existing is not None:
        return existing
    if not is_writer(session):
        raise RuntimeError("ensure_account needs a writer session; use session_scope(write=True)")
    account = LinkedInAccount(user_id=user.id, label=DEFAULT_ACCOUNT_LABEL)
    session.add(account)
    session.flush()
    log.info("created linkedin account %d for user %d", account.id, user.id)
    return account


def local_account_id(session: Session) -> int | None:
    """The account of the first local user, or ``None`` before there is one. Read-only."""
    user = local_owner(session)
    if user is None:
        return None
    account = find_account(session, user)
    return None if account is None else account.id


def account_id_for(session: Session, user: User) -> int:
    """The id ``user``'s budgets and heat are keyed by, without creating anything.

    For read-only callers (``netkeep posture``): the row's id when there is
    one, else :data:`LEGACY_ACCOUNT_ID`.
    """
    account = find_account(session, user)
    return LEGACY_ACCOUNT_ID if account is None else account.id


# --- scheduled runs: disarmed until a person arms them (P2-10) ----------------


def scheduled_runs_armed(session: Session, user: User, account_id: int) -> bool:
    """Whether scheduled LinkedIn runs may fire for ``account_id``. Read-only.

    ``False`` unless a person armed them (:func:`arm_scheduled_runs`), and
    ``False`` for an account id ``user`` has no row for: nothing that cannot
    be found can have been armed. Only ``linkedin_accounts.scheduled_runs_armed_at``
    decides; no config value or ``settings_kv`` key can.
    """
    armed_at = session.scalar(
        scoped(user, LinkedInAccount)
        .with_only_columns(LinkedInAccount.scheduled_runs_armed_at)
        .where(LinkedInAccount.id == account_id)
    )
    return armed_at is not None


def arm_scheduled_runs(session: Session, user: User, *, now: datetime) -> LinkedInAccount:
    """Let ``user``'s account's scheduled runs fire. A person's act, never a default.

    Arming an armed account changes nothing (it keeps the first arming time). Arming
    takes the account out of Off (#829): Off is always disarmed.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    _require_writer(session, "arm_scheduled_runs")
    account = ensure_account(session, user)
    if account.automation_off_at is not None:
        account.automation_off_at = None
        account.automation_changed_at = now
    if account.scheduled_runs_armed_at is None:
        account.scheduled_runs_armed_at = now
        account.automation_changed_at = now
        log.warning("scheduled LinkedIn runs ARMED for account %d (user %d)", account.id, user.id)
    return account


def disarm_scheduled_runs(
    session: Session, user: User, *, now: datetime | None = None
) -> LinkedInAccount:
    """Stop ``user``'s account's scheduled runs from firing. Manual runs are unaffected.
    ``now``, when given, dates the change for the automation switch (#829)."""
    _require_writer(session, "disarm_scheduled_runs")
    account = ensure_account(session, user)
    if account.scheduled_runs_armed_at is not None:
        account.scheduled_runs_armed_at = None
        if now is not None:
            account.automation_changed_at = now
        log.warning(
            "scheduled LinkedIn runs disarmed for account %d (user %d)", account.id, user.id
        )
    return account


# --- the automation switch's own state (#829) -----------------------------------


def full_upkeep(session: Session, user: User, account_id: int) -> bool:
    """Whether ``account_id``'s schedule fires the full upkeep kinds (the full sync and
    enrichment), not only the quick checks. Read-only. ``False`` for an account id
    ``user`` has no row for: the side that runs less."""
    depth = session.scalar(
        scoped(user, LinkedInAccount)
        .with_only_columns(LinkedInAccount.scheduled_depth)
        .where(LinkedInAccount.id == account_id)
    )
    return depth is ScheduleDepth.FULL


def linkedin_off(session: Session, user: User) -> bool:
    """Whether a person turned LinkedIn Off (#829): runs a person starts are refused too.
    Read-only. ``False`` before the account has a row: nobody can have turned it off."""
    account = find_account(session, user)
    return account is not None and account.automation_off_at is not None


def _require_writer(session: Session, where: str) -> None:
    if not is_writer(session):
        raise RuntimeError(f"{where} needs a writer session; use session_scope(write=True)")
