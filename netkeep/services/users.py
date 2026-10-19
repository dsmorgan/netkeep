"""User rows. v1 has exactly one, of kind ``local`` (spec section 5, ADR 0005)."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from netkeep.config import Settings
from netkeep.models import User, UserKind

log = logging.getLogger(__name__)


def local_owner(session: Session) -> User | None:
    """The owner account: the first ``local`` user, or None before one exists.

    The one query behind "the local user" for anything that acts on the whole install
    rather than on one user's rows: the nightly backup's settings, the ``/backups`` API.
    """
    users = local_users(session)
    return users[0] if users else None


def local_users(session: Session) -> list[User]:
    """Every ``local`` user, by id. The first is :func:`local_owner`."""
    return list(session.scalars(select(User).where(User.kind == UserKind.LOCAL).order_by(User.id)))


def ensure_local_user(session: Session, *, settings: Settings | None = None) -> User:
    """Return the local user, creating it on first start and keeping its timezone current.

    Idempotent. The timezone of a new user comes from
    ``settings.linkedin.timezone`` when settings are given, else ``UTC``.

    **``linkedin.timezone`` is the single source of truth for the account's
    zone, and this is what makes that true after the first start too.** It used
    to seed a new row only, so editing ``linkedin.timezone`` in ``config.toml``
    after ``netkeep db upgrade`` left the two permanently disagreeing -- and
    they are read by different things. ``netkeep.services.budgets`` keys its
    per-day and per-week counters off ``User.timezone``; the active window
    (spec 9.5) and the scheduler's deferral read ``linkedin.timezone``. Two
    zones means the daily budget resets at an hour the active window is open,
    which is a second budget-day inside one calendar day: exactly the defect
    spec 9.6's local-day acceptance test exists to prevent, arrived at from the
    other direction. Resyncing on every start makes them unable to disagree,
    and ``netkeep.services.posture`` keeps its own check of the two as a
    regression detector rather than as the fix.

    The zone in force is the file's, else the Settings page's (#597), else the
    default: the page's value is laid over ``settings`` here, so a start never puts
    the row back to the file's zone after the page changed it.

    The user row stays the thing budgets read, rather than budgets learning to
    read the config, because in a hosted deployment the zone belongs to the
    user and not to one machine's config file (ADR 0005). This writes, so it
    needs the writer session every caller already uses.
    """
    existing = local_owner(session)
    if existing is not None:
        if settings is not None:
            # Imported here: ui_settings reaches linkedin_accounts, which needs local_owner.
            from netkeep.services import ui_settings

            ui_settings.sync_timezone(
                session, existing, ui_settings.resolve(session, existing, settings)
            )
        return existing
    timezone = settings.linkedin.timezone if settings is not None else "UTC"
    user = User(kind=UserKind.LOCAL, timezone=timezone)
    session.add(user)
    session.flush()
    log.info("created local user %d with timezone %s", user.id, timezone)
    return user
