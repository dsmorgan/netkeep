"""How often LinkedIn runs changed contacts' job fields, as counts only (#518).

Sync and enrichment both write ``headline`` with source ``sync``: the sync from a
connections-page card, enrichment from a profile's top card. When the two read a
different text for the same person, each run puts its own back, and a headline
flips between them. This module counts that from what the runs left behind,
``contact_snapshots``, so it answers for runs recorded before their counts carried
``field_changes`` too.

A snapshot holds a contact's job fields *before* a change. The value after it is
the next snapshot's, or the contact's live value for the newest one. A field
changed in a snapshot when its before value was not empty and differs from the
after value. A snapshot belongs to a run when the run wrote it: source ``sync``,
observed between the run's start and its end (now, for a run still going). The
counts are approximate where a change wrote no snapshot (a person's edit, a merge,
a card-only contact's first confirmation): the value after the snapshot before it
is then the later one.

A **flip-flop** is a contact whose headline, in a later run, changed back to a
value it held before a change in an earlier run (A, then B, then A again). The
report holds numbers only: no name, no contact id, no headline text.

Read-only, and every query is scoped to the user.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import or_
from sqlalchemy.orm import Session

from netkeeper.crm.identity import JOB_FIELD_KEYS, JOB_FIELDS
from netkeeper.models import Contact, ContactSnapshot, ContactSource, User
from netkeeper.scoping import scoped, scoped_contacts
from netkeeper.services import runs


@dataclass(frozen=True, slots=True)
class RunFieldChanges:
    """One run's snapshots, and how many of them changed each job field."""

    run_id: int
    kind: str
    started_at: datetime
    snapshots: int
    field_changes: dict[str, int]


@dataclass(frozen=True, slots=True)
class FieldChangesReport:
    """The runs, oldest first, and the contacts whose headline flip-flopped across them."""

    runs: tuple[RunFieldChanges, ...]
    headline_flip_flops: int


@dataclass(frozen=True, slots=True)
class _Change:
    run_index: int
    before: str
    after: str | None


def field_changes(
    session: Session, user: User, run_ids: Sequence[int], *, now: datetime | None = None
) -> FieldChangesReport:
    """Count each run's snapshots and field changes, and the headline flip-flops across them.

    Runs are ordered by when they started, whatever order ``run_ids`` gives; a
    repeated id counts once. :class:`~netkeeper.services.runs.RunNotFound` for an
    id that is not one of ``user``'s runs. Read-only.
    """
    now = datetime.now(UTC) if now is None else now
    chosen = sorted(
        {run.id: run for run in (runs.get_run(session, user, rid) for rid in run_ids)}.values(),
        key=lambda run: (run.started_at, run.id),
    )
    windows = [(run.started_at, run.completed_at or now) for run in chosen]
    in_any = [ContactSnapshot.observed_at.between(start, end) for start, end in windows]
    touched = (
        set(
            session.scalars(
                scoped(user, ContactSnapshot)
                .with_only_columns(ContactSnapshot.contact_id)
                .where(ContactSnapshot.source == ContactSource.SYNC, or_(*in_any))
            )
        )
        if in_any
        else set()
    )
    timelines: dict[int, list[ContactSnapshot]] = defaultdict(list)
    if touched:
        statement = (
            scoped(user, ContactSnapshot)
            .where(ContactSnapshot.contact_id.in_(touched))
            .order_by(ContactSnapshot.observed_at, ContactSnapshot.id)
        )
        for snap in session.scalars(statement):
            timelines[snap.contact_id].append(snap)
    live = (
        {
            contact.id: contact
            for contact in session.scalars(scoped_contacts(user).where(Contact.id.in_(touched)))
        }
        if touched
        else {}
    )

    snapshots = [0] * len(chosen)
    changes = [dict.fromkeys(JOB_FIELD_KEYS.values(), 0) for _ in chosen]
    flip_flops = 0
    for contact_id, timeline in timelines.items():
        headline_changes: list[_Change] = []
        for position, snap in enumerate(timeline):
            index = _run_of(snap, windows)
            if index is None:
                continue
            snapshots[index] += 1
            after: ContactSnapshot | Contact | None = (
                timeline[position + 1] if position + 1 < len(timeline) else live.get(contact_id)
            )
            for name in JOB_FIELDS:
                before = getattr(snap, name)
                value = None if after is None else getattr(after, name)
                if before in (None, "") or before == value:
                    continue
                changes[index][JOB_FIELD_KEYS[name]] += 1
                if name == "headline":
                    headline_changes.append(_Change(index, before, value))
        if _flip_flopped(headline_changes):
            flip_flops += 1
    return FieldChangesReport(
        runs=tuple(
            RunFieldChanges(
                run_id=run.id,
                kind=run.kind.value,
                started_at=run.started_at,
                snapshots=snapshots[index],
                field_changes=changes[index],
            )
            for index, run in enumerate(chosen)
        ),
        headline_flip_flops=flip_flops,
    )


def _run_of(snap: ContactSnapshot, windows: list[tuple[datetime, datetime]]) -> int | None:
    """The first chosen run that wrote ``snap``, or None when none of them did."""
    if snap.source is not ContactSource.SYNC:
        return None
    for index, (start, end) in enumerate(windows):
        if start <= snap.observed_at <= end:
            return index
    return None


def _flip_flopped(changes: list[_Change]) -> bool:
    """True when a change in a later run went back to a value an earlier run changed away."""
    return any(
        later.run_index > earlier.run_index and later.after == earlier.before
        for earlier in changes
        for later in changes
    )
