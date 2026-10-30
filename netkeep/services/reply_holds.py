"""Why a held Gmail follow-up waits: the per-mailbox hold record (#716).

Since #694 the campaign tick holds a mailbox's Gmail follow-ups while its replies have
not been read recently (``GmailSender.replies_held``), and leaves the enrollments as
they are. The hold itself lives in the running sender (its catch-up set, the user's
poll interval), so a read elsewhere can't work it out. This module keeps a record of
it per mailbox (:class:`~netkeep.models.MailboxReplyHold`), for the campaign page and
the dashboard to say why a due follow-up waits.

Display only. Nothing that decides a send reads the record:

- The tick's chooser collects each mailbox's answer as it asks the sender's hold, and
  the tick hands those answers to :func:`keep` in its own writer session after the
  tick's send is recorded (``campaign_engine.tick_user``). A failure there is logged
  and changes nothing the tick did.
- A held answer writes the record; a clear answer deletes it. An answer the record
  already says writes nothing, so a hold that lasts many ticks costs one write.
- A reply poll that read everything after the hold started (``replies_polled_at``
  later than ``held_since``) lifts the hold on its own, and the record reads as lifted
  (:func:`current`) until the next tick deletes it or starts it again. The poll's own
  writes don't change.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Final

from sqlalchemy.orm import Session

from netkeep.db import is_writer
from netkeep.models import Mailbox, MailboxReplyHold, User
from netkeep.models.mailboxes import REPLY_HOLD_REASON_MAX_LENGTH
from netkeep.scoping import get_scoped, scoped

log = logging.getLogger(__name__)

SENT_NOTHING: Final = "; nothing was sent"
"""How a held send's error ends. The record leaves it off: the campaign page shows the
reason under "Held until replies are read", which looks ahead, not back."""


def current(hold: MailboxReplyHold, replies_polled_at: datetime | None) -> bool:
    """Whether ``hold`` still stands: no poll has read everything since it started."""
    return replies_polled_at is None or replies_polled_at <= hold.held_since


def _records(session: Session, user: User, mailbox_ids: list[int]) -> dict[int, MailboxReplyHold]:
    rows = session.scalars(
        scoped(user, MailboxReplyHold).where(MailboxReplyHold.mailbox_id.in_(mailbox_ids))
    )
    return {row.mailbox_id: row for row in rows}


def changes(
    session: Session, user: User, answers: Mapping[int, str | None]
) -> dict[int, str | None]:
    """The answers (mailbox id: the sender's reason, or None for not held) that the
    stored records don't already say. A read: :func:`keep` writes them."""
    if not answers:
        return {}
    records = _records(session, user, sorted(answers))
    changed: dict[int, str | None] = {}
    for mailbox_id, reason in answers.items():
        hold = records.get(mailbox_id)
        if reason is None:
            if hold is not None:
                changed[mailbox_id] = None
            continue
        mailbox = get_scoped(session, user, Mailbox, mailbox_id)
        if mailbox is None:
            continue
        if (
            hold is None
            or hold.reason != _fit(reason)
            or not current(hold, mailbox.replies_polled_at)
        ):
            changed[mailbox_id] = reason
    return changed


def keep(session: Session, user: User, answers: Mapping[int, str | None], *, now: datetime) -> None:
    """Make the records say ``answers``: a reason starts (or restarts) the mailbox's hold
    at ``now``, None deletes it. Only the answers :func:`changes` finds are written.
    Needs a writer session: it reads, then writes."""
    if not is_writer(session):
        raise RuntimeError(
            "reply_holds.keep needs a writer session; use session_scope(factory, write=True)"
        )
    changed = changes(session, user, answers)
    if not changed:
        return
    records = _records(session, user, sorted(changed))
    for mailbox_id, reason in changed.items():
        hold = records.get(mailbox_id)
        if reason is None:
            if hold is not None:
                session.delete(hold)
                log.info("mailbox %d: its follow-ups are no longer held", mailbox_id)
            continue
        if hold is None:
            hold = MailboxReplyHold(user_id=user.id, mailbox_id=mailbox_id)
            session.add(hold)
        hold.reason = _fit(reason)
        hold.held_since = now
        log.info("mailbox %d: its follow-ups are held: %s", mailbox_id, hold.reason)
    session.flush()


def _fit(reason: str) -> str:
    """The reason as stored: without :data:`SENT_NOTHING`, and within the column."""
    return reason.removesuffix(SENT_NOTHING)[:REPLY_HOLD_REASON_MAX_LENGTH]
