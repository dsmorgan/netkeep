"""What the dashboard reads that no other endpoint already answers (P3-12, spec 14.3).

The dashboard asks "what happens next, and is anything unhealthy?" Most of the
answer already has an endpoint: the LinkedIn schedule, budget, heat, session
flag and runs (``/linkedin/*``, P2-10), and the mailbox (``/mailboxes``,
#271). The page reads those as they are, so nothing here restates their math.
This module holds the reads that had no home:

- **Changed jobs** (spec 9.8, "the single best reason to reconnect"): live
  contacts with a ``contact_snapshot`` marked ``position_changed`` and written
  by the sync's own visits (source ``sync``) in the last
  :data:`CHANGED_JOBS_WINDOW`, dated by the snapshot's ``observed_at``: when
  netkeep noticed the change, not the start date on the profile, which
  people often fill in late (#286). A new headline or location alone is not a
  job change, and neither is a title or company an import wrote: an import
  restates a file, it does not notice anything. The ``last_position_change``
  merge field (spec 11.1) still reads the positions' dates; a template says
  "congrats on the move" by the profile's calendar. A contact who is archived,
  merged away, or do-not-contact is not a prompt to reach out. A contact's
  first enrichment is never a change: only a later enrichment that finds a
  different position than an earlier one recorded is (#323). The CRM filter
  ``changed_jobs_within_days`` shares the selection
  (:mod:`netkeep.crm.job_changes`).
- **Inbound this week**: interactions of kind ``email_in`` or ``li_in`` in the
  last seven days. This is not a reply count. Reply detection is P3-08 and is
  not built yet, so the dashboard says so, and shows this count as what it is.

- **Metrics** (#738): five numbers over a rolling window that ends today, each
  with a series, from :func:`metrics`. Its docstring holds the definitions.

The next campaign fires are :func:`netkeep.services.campaign_engine.upcoming`,
which lives beside the tick so that "what the tick will fire" has one
definition.

Read-only, every one: a dashboard load never takes the write lock.
"""

from __future__ import annotations

import enum
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any, Final

from sqlalchemy import ColumnElement, Select, case, func, select
from sqlalchemy.orm import Session

from netkeep.crm.job_changes import job_change
from netkeep.localtime import find_zone, local_today
from netkeep.models import (
    Contact,
    ContactSnapshot,
    ContactSource,
    Enrollment,
    Interaction,
    InteractionKind,
    Message,
    MessageDirection,
    MessageStatus,
    User,
)
from netkeep.scoping import SCOPE_OPTION, scoped, scoped_contacts
from netkeep.services.campaign_results import WENT_OUT

log = logging.getLogger(__name__)

#: Spec 9.8: 'The dashboard surfaces "changed jobs in the last 30 days"'.
CHANGED_JOBS_DAYS: Final = 30
CHANGED_JOBS_WINDOW: Final = timedelta(days=CHANGED_JOBS_DAYS)

#: "This week" on the dashboard is the last seven days, not the calendar week,
#: so Monday morning does not read as an empty week.
INBOUND_WINDOW: Final = timedelta(days=7)

INBOUND_KINDS: Final = (
    InteractionKind.EMAIL_IN,
    InteractionKind.LI_IN,
    # A response a person recorded from outside netkeep (#512): a call, a meeting, a
    # text or another app. Them reaching you, as the two above are.
    InteractionKind.CALL_IN,
    InteractionKind.MEETING_IN,
    InteractionKind.OTHER_IN,
)


@dataclass(frozen=True, slots=True)
class ChangedJob:
    contact: Contact
    noticed_at: datetime


def changed_jobs(
    session: Session, user: User, *, now: datetime, limit: int
) -> tuple[list[ChangedJob], int]:
    """Live contacts netkeep saw change position in the last :data:`CHANGED_JOBS_WINDOW`.

    A change is what :func:`~netkeep.crm.job_changes.job_change` selects (a
    ``contact_snapshot`` with ``position_changed`` set and source ``sync``),
    observed between ``now - CHANGED_JOBS_WINDOW`` and ``now``, both inclusive:
    the same rows the filter ``changed_jobs_within_days`` matches. Each contact
    is listed once, at its latest such snapshot; newest first, then by contact
    id. The total is the count before ``limit``.
    """
    latest = (
        select(
            ContactSnapshot.contact_id.label("contact_id"),
            func.max(ContactSnapshot.observed_at).label("noticed_at"),
        )
        .where(job_change(user, since=now - CHANGED_JOBS_WINDOW, until=now))
        .group_by(ContactSnapshot.contact_id)
        .subquery()
    )
    statement = (
        scoped_contacts(user)
        .join(latest, latest.c.contact_id == Contact.id)
        .where(
            Contact.archived_at.is_(None),
            Contact.merged_into_id.is_(None),
            Contact.do_not_contact.is_(False),
        )
    )
    total = session.scalar(statement.with_only_columns(func.count(Contact.id)).order_by(None))
    rows = session.execute(
        statement.add_columns(latest.c.noticed_at)
        .order_by(latest.c.noticed_at.desc(), Contact.id)
        .limit(limit)
    )
    return [ChangedJob(contact=contact, noticed_at=at) for contact, at in rows], total or 0


def inbound_since(session: Session, user: User, *, now: datetime) -> tuple[int, datetime]:
    """Inbound interactions (email, LinkedIn, or a response recorded from outside) in the
    last :data:`INBOUND_WINDOW`, and its start.

    An interaction dated after ``now`` (a clock skew, a hand-typed date) is left
    out: "this week" is up to now.
    """
    since = now - INBOUND_WINDOW
    count = session.scalar(
        scoped(user, Interaction)
        .with_only_columns(func.count(Interaction.id))
        .where(
            Interaction.kind.in_(INBOUND_KINDS),
            Interaction.at >= since,
            Interaction.at <= now,
        )
    )
    return count or 0, since


# --- metrics (#738) --------------------------------------------------------------------


class MetricsWindow(enum.StrEnum):
    """A metrics window: the last 7 or 30 local days, or the last 12 local months, each
    including today."""

    DAYS_7 = "7d"
    DAYS_30 = "30d"
    YEAR = "1y"


_WINDOW_DAYS: Final = {MetricsWindow.DAYS_7: 7, MetricsWindow.DAYS_30: 30}
YEAR_MONTHS: Final = 12

LINKEDIN_SOURCES: Final = frozenset({ContactSource.SYNC})
"""A new contact a LinkedIn sync added. Every other first source (an archive or CSV
import, or one added by hand, which has no bucket of its own) counts as imported."""


@dataclass(frozen=True, slots=True)
class MetricsBucket:
    start: date
    """The bucket's first local day: the day itself, or the first of its month."""
    sent: int = 0
    replies: int = 0
    bounced: int = 0
    changed_jobs: int = 0
    new_contacts: int = 0


@dataclass(frozen=True, slots=True)
class Metrics:
    window: MetricsWindow
    start: date
    end: date
    """Today, in the user's time zone: the window's last day."""
    sent: int
    replies: int
    reply_rate: float | None
    bounced: int
    changed_jobs: int
    new_contacts: int
    new_contacts_linkedin: int
    new_contacts_imported: int
    series: tuple[MetricsBucket, ...]


def _bucket_starts(window: MetricsWindow, today: date) -> list[date]:
    """Each bucket's first day, oldest first, then the day after the last bucket ends."""
    if window is MetricsWindow.YEAR:
        months = [
            (today.year * 12 + today.month - 1) - back for back in range(YEAR_MONTHS - 1, -2, -1)
        ]
        return [date(m // 12, m % 12 + 1, 1) for m in months]
    days = _WINDOW_DAYS[window]
    return [today - timedelta(days=back) for back in range(days - 1, -2, -1)]


def _bucket_of(column: Any, edges: list[datetime]) -> ColumnElement[Any]:
    """The index of the bucket ``column`` falls in, from the bucket edges in UTC.

    A ``CASE`` over the edges rather than a date function: SQLite has no time zone
    database, and a fixed offset would put the hour around a daylight-saving change in
    the wrong day. Each edge is a local midnight computed in Python, so both databases
    run the same comparisons. Rows before ``edges[0]`` are the caller's to leave out.
    """
    return case(
        *((column < edge, index) for index, edge in enumerate(edges[1:-1])),
        else_=len(edges) - 2,
    )


def _grouped(
    session: Session, user: User, inner: Select[Any], *group: str
) -> list[tuple[Any, ...]]:
    """``COUNT(*)`` of ``inner``'s rows by its labeled columns ``group``.

    Grouped from a subquery, so ``GROUP BY`` names a column, not the ``CASE``
    again: PostgreSQL would see a repeated ``CASE`` with its own bound parameters as
    a different expression from the one selected. ``inner`` must be scoped to
    ``user`` (:func:`~netkeep.scoping.scoped`); the outer statement only counts its
    rows, so it carries the same scope for the guard.
    """
    sub = inner.subquery()
    columns = [sub.c[name] for name in group]
    statement = (
        select(*columns, func.count())
        .select_from(sub)
        .group_by(*columns)
        .execution_options(**{SCOPE_OPTION: user.id})
    )
    return [tuple(row) for row in session.execute(statement)]


def metrics(session: Session, user: User, window: MetricsWindow, *, now: datetime) -> Metrics:
    """The dashboard's five numbers over ``window``, with one bucket per day (``7d``,
    ``30d``) or per month (``1y``), in the user's time zone.

    The window runs from the local midnight that starts its first day up to ``now``:
    anything dated after ``now`` (a clock skew, a hand-typed date) is left out, as
    "inbound this week" leaves it out. A time zone that cannot be read counts UTC days,
    as :func:`~netkeep.localtime.local_today` does.

    - **sent**: outbound campaign messages that went out (status ``sent``, or
      ``bounced``, which was sent first), email and LinkedIn alike, by ``sent_at``. The
      same rows a campaign's results count as sends
      (:data:`~netkeep.services.campaign_results.WENT_OUT`). A test send is not one.
    - **replies**: enrollments whose first response, ``replied_at``, falls in the
      window: a reply either poll found, an opt-out, or a response recorded from
      outside netkeep (#512). An enrollment counts once, at its first response, as a
      campaign's results count it.
    - **reply_rate**: ``replies / sent``, from the window's own two counts; None when
      nothing was sent. A reply in the window can answer a message sent before it, so
      on a short window the rate can pass 1.
    - **bounced**: outbound messages marked ``bounced``, by ``bounced_at`` (``sent_at``
      for a row with none).
    - **changed_jobs**: contacts with a job change noticed in the window, as
      :func:`~netkeep.crm.job_changes.job_change` defines one (a sync's snapshot marked
      ``position_changed``: the title or company changed), and live, not merged away
      and not do-not-contact: the contacts the changed-jobs card lists. A contact
      counts once in the total and once in each bucket it changed in, so the buckets
      can add up to more than the total.
    - **new_contacts**: contacts created in the window (``Contact.created_at``), less
      the self contact and contacts merged into another (a duplicate is not a new
      person); an archived one still counts. ``linkedin`` is first source ``sync``
      (a LinkedIn connections sync); ``imported`` is every other first source: an
      archive or CSV import, and also a contact added by hand, since the response
      has no bucket for those.

    Six statements, whatever the window: each groups its rows by bucket in the
    database.
    """
    zone: tzinfo = find_zone(user.timezone) or UTC
    today = local_today(user.timezone, now)
    starts = _bucket_starts(window, today)
    edges = [datetime(d.year, d.month, d.day, tzinfo=zone).astimezone(UTC) for d in starts]
    since = edges[0]

    def within(column: Any) -> tuple[ColumnElement[bool], ...]:
        return (column >= since, column <= now)

    def per_bucket(inner: Select[Any]) -> Counter[int]:
        return Counter(dict(_grouped(session, user, inner, "bucket")))

    sent = per_bucket(
        scoped(user, Message)
        .with_only_columns(_bucket_of(Message.sent_at, edges).label("bucket"))
        .where(
            Message.direction == MessageDirection.OUT,
            Message.status.in_(WENT_OUT),
            *within(Message.sent_at),
        )
    )
    bounced_at = func.coalesce(Message.bounced_at, Message.sent_at)
    bounced = per_bucket(
        scoped(user, Message)
        .with_only_columns(_bucket_of(bounced_at, edges).label("bucket"))
        .where(
            Message.direction == MessageDirection.OUT,
            Message.status == MessageStatus.BOUNCED,
            *within(bounced_at),
        )
    )
    replies = per_bucket(
        scoped(user, Enrollment)
        .with_only_columns(_bucket_of(Enrollment.replied_at, edges).label("bucket"))
        .where(*within(Enrollment.replied_at))
    )

    changes = (
        scoped(user, ContactSnapshot)
        .with_only_columns(
            ContactSnapshot.contact_id.label("contact_id"),
            _bucket_of(ContactSnapshot.observed_at, edges).label("bucket"),
        )
        .join(Contact, Contact.id == ContactSnapshot.contact_id)
        .where(
            job_change(user, since=since, until=now),
            Contact.user_id == user.id,
            Contact.archived_at.is_(None),
            Contact.merged_into_id.is_(None),
            Contact.do_not_contact.is_(False),
        )
        .distinct()
    )
    changed = Counter(dict(_grouped(session, user, changes, "bucket")))
    distinct = changes.with_only_columns(ContactSnapshot.contact_id).distinct().subquery()
    changed_total = (
        session.scalar(
            select(func.count()).select_from(distinct).execution_options(**{SCOPE_OPTION: user.id})
        )
        or 0
    )

    created: Counter[int] = Counter()
    linkedin = imported = 0
    for index, source, count in _grouped(
        session,
        user,
        scoped_contacts(user)
        .with_only_columns(
            _bucket_of(Contact.created_at, edges).label("bucket"), Contact.source.label("source")
        )
        .where(Contact.merged_into_id.is_(None), *within(Contact.created_at)),
        "bucket",
        "source",
    ):
        created[index] += count
        if ContactSource(source) in LINKEDIN_SOURCES:
            linkedin += count
        else:
            imported += count

    total_sent = sum(sent.values())
    total_replies = sum(replies.values())
    return Metrics(
        window=window,
        start=starts[0],
        end=today,
        sent=total_sent,
        replies=total_replies,
        reply_rate=total_replies / total_sent if total_sent else None,
        bounced=sum(bounced.values()),
        changed_jobs=changed_total,
        new_contacts=linkedin + imported,
        new_contacts_linkedin=linkedin,
        new_contacts_imported=imported,
        series=tuple(
            MetricsBucket(
                start=start,
                sent=sent[index],
                replies=replies[index],
                bounced=bounced[index],
                changed_jobs=changed[index],
                new_contacts=created[index],
            )
            for index, start in enumerate(starts[:-1])
        ),
    )
