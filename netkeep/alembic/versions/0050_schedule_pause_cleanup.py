"""Clean up any LinkedIn schedule pause key that 0049 couldn't reach (#834).

Migration 0049 (#829) deleted every schedule pause key (``linkedin.schedule.<id>.paused_at``
in ``settings_kv``) and moved armed, paused accounts to Manual. For about 33 minutes
afterwards (#830 merged at 2026-11-16 23:32 UTC, #833 at 00:05 UTC), ``main`` was at 0049
while the CLI's ``linkedin schedule pause`` and ``POST /linkedin/schedule/pause`` could
still write a key. A key written then is past 0049, and so is one in a backup taken then, since
a restore of a backup already at 0049 doesn't run 0049 again. #834 removes every read of
the key, so such a key would stop holding an armed account's schedule.

This migration repeats 0049's cleanup once more:

- An armed account with a pause key is disarmed, which moves it to Manual, as 0049 did.
- Every pause key is deleted, including one whose account doesn't exist.

An armed account with no key keeps Automatic, and nothing else changes. The downgrade
does nothing: the keys are gone for good, and an account it disarmed stays on Manual.
Both directions run less, never more.

Revision ID: 0050
Revises: 0049
Create Date: 2026-11-22 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0050"
down_revision: str | None = "0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE: Final = "linkedin_accounts"
#: Every pause key. ``_`` is a LIKE wildcard, so the one in ``paused_at`` is escaped.
KEY_PATTERN: Final = "linkedin.schedule.%.paused!_at"


def _pause_key(account_id: int) -> str:
    return f"linkedin.schedule.{account_id}.paused_at"


def upgrade() -> None:
    connection = op.get_bind()
    armed = connection.execute(
        sa.text(
            f"SELECT id, user_id FROM {TABLE}"  # noqa: S608
            " WHERE scheduled_runs_armed_at IS NOT NULL"
        )
    ).all()
    for account_id, user_id in armed:
        paused = connection.execute(
            sa.text("SELECT 1 FROM settings_kv WHERE user_id = :user_id AND key = :key"),
            {"user_id": user_id, "key": _pause_key(account_id)},
        ).first()
        if paused is not None:
            # Armed and paused: Manual.
            connection.execute(
                sa.text(
                    f"UPDATE {TABLE} SET scheduled_runs_armed_at = NULL"  # noqa: S608
                    " WHERE id = :id"
                ),
                {"id": account_id},
            )
    connection.execute(
        sa.text("DELETE FROM settings_kv WHERE key LIKE :pattern ESCAPE '!'"),
        {"pattern": KEY_PATTERN},
    )


def downgrade() -> None:
    # Nothing to put back: a pause key is never restored, and Manual runs less.
    pass
