"""A response from outside the app: how, what it said, and its timeline kinds (#512).

A contact can answer a campaign by phone, by text, in person or on another app, where
neither poll sees it. A person records it, and it is stored as a reply is: an inbound
``messages`` row on each enrollment it ends, and one interaction.

- ``messages.outside_via`` is how they responded (``call``, ``text``, ``other_email``,
  ``other_app``, ``in_person``, ``other``), and ``messages.outside_outcome`` what it
  said (``positive``, ``neutral``, ``not_interested``, ``opt_out``). Both NULL for
  every existing row: everything stored so far came from a poll or was sent.
- ``interactions.kind`` takes ``call_in``, ``meeting_in`` and ``other_in``, so the
  timeline shows the contact reaching you, not you reaching them.

SQLite cannot add or replace a CHECK constraint on a table that exists, so both tables
go through ``batch_alter_table``. The one partial index on ``messages`` is dropped first
and made again after, as 0039 does: a rebuild would not keep its ``WHERE``.

No existing data changes. The downgrade turns the three new kinds into ``note``, which
the old CHECK takes and which, like them, is not outbound, then drops the columns; the
inbound messages stay, as replies.

Revision ID: 0042
Revises: 0041
Create Date: 2026-10-14 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MESSAGES: Final = "messages"
INTERACTIONS: Final = "interactions"
OPEN_INDEX: Final = "uq_messages_one_open_prefill"
OPEN_WHERE: Final = (
    "channel = 'linkedin' AND direction = 'out' AND status IN ('scheduled', 'prefilled')"
    " AND send_clicked_at IS NULL"
)
VIA: Final = "outside_via IN ('call', 'text', 'other_email', 'other_app', 'in_person', 'other')"
OUTCOME: Final = "outside_outcome IN ('positive', 'neutral', 'not_interested', 'opt_out')"
KIND_BEFORE: Final = (
    "kind IN ('note', 'call', 'meeting', 'email_out', 'email_in', 'li_out', 'li_in', 'li_view')"
)
KIND_AFTER: Final = (
    "kind IN ('note', 'call', 'meeting', 'email_out', 'email_in', 'li_out', 'li_in', 'li_view',"
    " 'call_in', 'meeting_in', 'other_in')"
)
KIND_CONSTRAINT: Final = "interaction_kind"
"""The bare name; ``batch_alter_table`` applies the ``ck_<table>_<name>`` convention."""


def _open_index() -> None:
    op.create_index(
        OPEN_INDEX,
        MESSAGES,
        ["user_id"],
        unique=True,
        sqlite_where=sa.text(OPEN_WHERE),
        postgresql_where=sa.text(OPEN_WHERE),
    )


def upgrade() -> None:
    op.drop_index(OPEN_INDEX, table_name=MESSAGES)
    with op.batch_alter_table(MESSAGES) as batch:
        batch.add_column(sa.Column("outside_via", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("outside_outcome", sa.String(length=16), nullable=True))
        batch.create_check_constraint("message_outside_via", VIA)
        batch.create_check_constraint("message_outside_outcome", OUTCOME)
    _open_index()

    with op.batch_alter_table(INTERACTIONS) as batch:
        batch.drop_constraint(KIND_CONSTRAINT, type_="check")
        batch.create_check_constraint(KIND_CONSTRAINT, KIND_AFTER)


def downgrade() -> None:
    op.execute(
        "UPDATE interactions SET kind = 'note' WHERE kind IN ('call_in', 'meeting_in', 'other_in')"
    )
    with op.batch_alter_table(INTERACTIONS) as batch:
        batch.drop_constraint(KIND_CONSTRAINT, type_="check")
        batch.create_check_constraint(KIND_CONSTRAINT, KIND_BEFORE)

    op.drop_index(OPEN_INDEX, table_name=MESSAGES)
    with op.batch_alter_table(MESSAGES) as batch:
        batch.drop_constraint("message_outside_outcome", type_="check")
        batch.drop_constraint("message_outside_via", type_="check")
        batch.drop_column("outside_outcome")
        batch.drop_column("outside_via")
    _open_index()
