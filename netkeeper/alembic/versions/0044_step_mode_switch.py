"""A LinkedIn step's mode can change on a running campaign (#536).

A person can now switch an active or paused campaign's LinkedIn step between
``prefill`` and ``auto_send``. Work already queued keeps the mode it had, and every
switch is recorded. Two changes:

- ``enrollments.prefill_kept_step_id``: the step an enrollment keeps as a prefill,
  because it was already queued for that step when the step switched to
  ``auto_send``. ``SET NULL`` when the step is deleted.
- ``campaign_step_mode_changes``: one row per switch, with the step, the mode before
  and after, when, who, how many queued enrollments kept the step as a prefill, and
  whether ``[campaigns] linkedin_auto_send`` was on. Never message text.

No existing data changes. No step has switched yet, so no enrollment keeps one, and
the history starts empty. A downgrade first sets every ``auto_send`` step that any
enrollment keeps as a prefill back to ``prefill``, so dropping the column can never make
a kept contact auto-sendable, and then drops both. Other ``auto_send`` steps stay on it.

Revision ID: 0044
Revises: 0043
Create Date: 2026-10-14 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENROLLMENTS: Final = "enrollments"
COLUMN: Final = "prefill_kept_step_id"
STEP_FK: Final = "fk_enrollments_prefill_kept_step_id_campaign_steps"
TABLE: Final = "campaign_step_mode_changes"
# Enums are strings plus a CHECK, identical on SQLite and PostgreSQL (0018's step_mode).
FROM_MODE: Final = "from_mode IN ('draft', 'send', 'prefill', 'auto_send')"
TO_MODE: Final = "to_mode IN ('draft', 'send', 'prefill', 'auto_send')"


def upgrade() -> None:
    with op.batch_alter_table(ENROLLMENTS) as batch:
        batch.add_column(sa.Column(COLUMN, sa.Integer(), nullable=True))
        batch.create_foreign_key(STEP_FK, "campaign_steps", [COLUMN], ["id"], ondelete="SET NULL")

    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("step_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("from_mode", sa.String(length=16), nullable=False),
        sa.Column("to_mode", sa.String(length=16), nullable=False),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        sa.Column("changed_by", sa.Integer(), nullable=False),
        sa.Column("kept_prefill", sa.Integer(), nullable=False),
        sa.Column("auto_send_on", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(FROM_MODE, name=op.f(f"ck_{TABLE}_mode_change_from")),
        sa.CheckConstraint(TO_MODE, name=op.f(f"ck_{TABLE}_mode_change_to")),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
            name=op.f(f"fk_{TABLE}_campaign_id_campaigns"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["step_id"],
            ["campaign_steps.id"],
            name=op.f(f"fk_{TABLE}_step_id_campaign_steps"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f(f"fk_{TABLE}_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{TABLE}")),
    )
    for column in ("user_id", "campaign_id", "step_id"):
        op.create_index(op.f(f"ix_{TABLE}_{column}"), TABLE, [column], unique=False)


def downgrade() -> None:
    # Without the column a kept contact would follow its step's mode: put any step that
    # a contact keeps as a prefill back on prefill first, which only ever sends less.
    op.execute(
        sa.text(
            "UPDATE campaign_steps SET mode = 'prefill' WHERE mode = 'auto_send'"
            " AND EXISTS (SELECT 1 FROM enrollments e"
            "  WHERE e.prefill_kept_step_id = campaign_steps.id)"
        )
    )
    op.drop_table(TABLE)
    with op.batch_alter_table(ENROLLMENTS) as batch:
        batch.drop_constraint(STEP_FK, type_="foreignkey")
        batch.drop_column(COLUMN)
