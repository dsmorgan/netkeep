"""A sent or received message has a ``sent_at``; the retired review schema goes (#706).

Two changes, both to tables the code already treats this way:

* ``messages`` gets a CHECK, ``message_sent_at``: a ``sent`` or ``received`` message has
  a ``sent_at`` (the column is its send time, or for a reply its arrival time). Every
  writer sets it, but nothing held the database to that. The upgrade first looks for
  rows that break the rule and gives each one the best time the row has, its
  ``updated_at`` (the last change, which moved it to ``sent`` or ``received``), so the
  CHECK never refuses an existing database. No such row is expected.
* The review gate's first design is gone. ``campaign_review_previews`` (0024) is no
  longer read or written, and neither are the ``guards_*`` columns on ``campaigns``
  (0024; the guard acknowledgement was retired in #346). Both are dropped. The downgrade
  recreates them empty; the rows held only previews that nothing reads.

Revision ID: 0047
Revises: 0046
Create Date: 2026-10-20 00:00:00 UTC
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

logger = logging.getLogger("alembic.runtime.migration")

revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SENT_AT_CONSTRAINT: Final = "message_sent_at"
"""The bare name; ``batch_alter_table`` applies the ``ck_<table>_<name>`` convention."""
SENT_AT_RULE: Final = "status NOT IN ('sent', 'received') OR sent_at IS NOT NULL"

PREVIEWS: Final = "campaign_review_previews"
GUARD_COLUMNS: Final = (
    ("guards_acknowledged_at", sa.DateTime()),
    ("guards_fingerprint", sa.String(length=64)),
    ("guards_summary", sa.Text()),
)


def upgrade() -> None:
    dated = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT COUNT(*) FROM messages"
                " WHERE status IN ('sent', 'received') AND sent_at IS NULL"
            )
        )
        .scalar()
    )
    if dated:
        logger.warning("0047: dating %s sent or received messages that had no sent_at", dated)
    op.execute(
        "UPDATE messages SET sent_at = updated_at"
        " WHERE status IN ('sent', 'received') AND sent_at IS NULL"
    )
    with op.batch_alter_table("messages") as batch:
        batch.create_check_constraint(SENT_AT_CONSTRAINT, SENT_AT_RULE)

    for column in ("enrollment_id", "campaign_id", "user_id"):
        op.drop_index(op.f(f"ix_{PREVIEWS}_{column}"), table_name=PREVIEWS)
    op.drop_table(PREVIEWS)
    with op.batch_alter_table("campaigns") as batch:
        for name, _type in GUARD_COLUMNS:
            batch.drop_column(name)


def downgrade() -> None:
    with op.batch_alter_table("campaigns") as batch:
        for name, type_ in GUARD_COLUMNS:
            batch.add_column(sa.Column(name, type_, nullable=True))
    op.create_table(
        PREVIEWS,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("enrollment_id", sa.Integer(), nullable=False),
        sa.Column("sampled", sa.Boolean(), nullable=False),
        sa.Column("sample_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("viewed_at", sa.DateTime(), nullable=False),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("approved_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
            name=op.f(f"fk_{PREVIEWS}_campaign_id_campaigns"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["enrollment_id"],
            ["enrollments.id"],
            name=op.f(f"fk_{PREVIEWS}_enrollment_id_enrollments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f(f"fk_{PREVIEWS}_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{PREVIEWS}")),
        sa.UniqueConstraint(
            "user_id",
            "campaign_id",
            "enrollment_id",
            name=op.f(f"uq_{PREVIEWS}_user_id_campaign_id_enrollment_id"),
        ),
    )
    for column in ("user_id", "campaign_id", "enrollment_id"):
        op.create_index(op.f(f"ix_{PREVIEWS}_{column}"), PREVIEWS, [column], unique=False)

    with op.batch_alter_table("messages") as batch:
        batch.drop_constraint(SENT_AT_CONSTRAINT, type_="check")
