"""Why a held Gmail follow-up waits: a per-mailbox hold record (#716).

Since #694 the campaign tick holds a mailbox's Gmail follow-ups while its replies have
not been read recently, and nothing on the enrollment says so. One new table:

- ``mailbox_reply_holds``: one row per mailbox whose follow-ups the tick holds, with the
  sender's reason and when the hold started. The tick writes it when a hold starts and
  deletes it when the hold lifts. The campaign page and the dashboard read it to say
  why each due follow-up on that mailbox waits. Nothing that decides a send reads it.

No existing data changes. The table starts empty, and the next tick that holds a
follow-up records its mailbox. The downgrade drops the table; it held only what the
next tick works out again.

Revision ID: 0048
Revises: 0047
Create Date: 2026-10-30 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE: Final = "mailbox_reply_holds"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("mailbox_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=200), nullable=False),
        sa.Column("held_since", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["mailbox_id"],
            ["mailboxes.id"],
            name=op.f(f"fk_{TABLE}_mailbox_id_mailboxes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f(f"fk_{TABLE}_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{TABLE}")),
        sa.UniqueConstraint("user_id", "mailbox_id", name=op.f(f"uq_{TABLE}_user_id_mailbox_id")),
    )
    op.create_index(op.f(f"ix_{TABLE}_user_id"), TABLE, ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_table(TABLE)
