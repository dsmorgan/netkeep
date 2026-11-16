"""One automation switch per service: Off, Manual, Automatic (#829).

LinkedIn's switch reuses arming: Automatic is ``scheduled_runs_armed_at`` set,
Manual is disarmed. Three columns on ``linkedin_accounts`` hold what arming can't:

- ``automation_off_at``: when a person turned LinkedIn Off. NULL while Manual or
  Automatic.
- ``scheduled_depth``: ``quick`` or ``full``, which kinds an armed schedule fires.
  Every existing row gets ``full``, which is what an armed schedule ran before.
- ``automation_changed_at``: when the switch last moved. NULL for every existing row.

The schedule pause (#324) goes away: Manual does its job. An armed account with a
pause key (``linkedin.schedule.<id>.paused_at`` in ``settings_kv``) is disarmed, so it
maps to Manual, and every pause key is deleted. An armed account without one maps to
Automatic with the full schedule; a disarmed one maps to Manual. Gmail needs no new
column: its switch is the mailboxes' existing arming (sending, drafts, not armed).

The downgrade first disarms every armed account at ``quick``: 0048's code knows no
depth and would fire the full sync and enrichment, which nobody confirmed. Then it drops
the three columns. It restores no pause, and Off becomes plain disarmed; both run less,
not more.

Revision ID: 0049
Revises: 0048
Create Date: 2026-11-16 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0049"
down_revision: str | None = "0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE: Final = "linkedin_accounts"
DEPTH_CONSTRAINT: Final = "scheduled_depth"
"""The bare name; ``batch_alter_table`` applies the ``ck_<table>_<name>`` convention."""
DEPTH_CHECK: Final = "scheduled_depth IN ('quick', 'full')"


def _pause_key(account_id: int) -> str:
    return f"linkedin.schedule.{account_id}.paused_at"


def upgrade() -> None:
    with op.batch_alter_table(TABLE) as batch:
        batch.add_column(sa.Column("automation_off_at", sa.DateTime(), nullable=True))
        batch.add_column(
            sa.Column(
                "scheduled_depth",
                sa.String(length=16),
                nullable=False,
                server_default="full",
            )
        )
        batch.add_column(sa.Column("automation_changed_at", sa.DateTime(), nullable=True))
        batch.create_check_constraint(DEPTH_CONSTRAINT, DEPTH_CHECK)

    connection = op.get_bind()
    accounts = connection.execute(
        sa.text(f"SELECT id, user_id, scheduled_runs_armed_at FROM {TABLE}")  # noqa: S608
    ).all()
    for account_id, user_id, armed_at in accounts:
        key = _pause_key(account_id)
        paused = connection.execute(
            sa.text("SELECT 1 FROM settings_kv WHERE user_id = :user_id AND key = :key"),
            {"user_id": user_id, "key": key},
        ).first()
        if paused is None:
            continue
        if armed_at is not None:
            # Armed and paused: Manual.
            connection.execute(
                sa.text(
                    f"UPDATE {TABLE} SET scheduled_runs_armed_at = NULL"  # noqa: S608
                    " WHERE id = :id"
                ),
                {"id": account_id},
            )
        connection.execute(
            sa.text("DELETE FROM settings_kv WHERE user_id = :user_id AND key = :key"),
            {"user_id": user_id, "key": key},
        )


def downgrade() -> None:
    # Quick checks only: without the depth column, armed would mean full upkeep.
    op.execute(
        f"UPDATE {TABLE} SET scheduled_runs_armed_at = NULL"  # noqa: S608
        " WHERE scheduled_depth = 'quick' AND scheduled_runs_armed_at IS NOT NULL"
    )
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_constraint(DEPTH_CONSTRAINT, type_="check")
        batch.drop_column("automation_changed_at")
        batch.drop_column("scheduled_depth")
        batch.drop_column("automation_off_at")
