"""A run kind for Check connection: ``session_check`` (#652).

``POST /linkedin/browser/check`` records a ``session_check`` run, which attaches to
the dedicated Chrome, reads the LinkedIn session from its cookie jar as the preflight
command does, and detaches. ``sync_runs.kind``'s CHECK takes the new value.

SQLite cannot replace a CHECK constraint on a table that exists, so the table goes
through ``batch_alter_table``, as 0042 does for ``interactions.kind``.

No existing data changes. The downgrade deletes the ``session_check`` runs, which the
old CHECK refuses and no other kind stands in for; they hold what one check read and
nothing else refers to them.

Revision ID: 0046
Revises: 0045
Create Date: 2026-10-18 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from alembic import op

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE: Final = "sync_runs"
KIND_BEFORE: Final = (
    "kind IN ('connections_full', 'connections_incremental', 'enrich', 'inbox', 'message_send')"
)
KIND_AFTER: Final = (
    "kind IN ('connections_full', 'connections_incremental', 'enrich', 'inbox', 'message_send',"
    " 'session_check')"
)
KIND_CONSTRAINT: Final = "sync_run_kind"
"""The bare name; ``batch_alter_table`` applies the ``ck_<table>_<name>`` convention."""


def upgrade() -> None:
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_constraint(KIND_CONSTRAINT, type_="check")
        batch.create_check_constraint(KIND_CONSTRAINT, KIND_AFTER)


def downgrade() -> None:
    op.execute("DELETE FROM sync_runs WHERE kind = 'session_check'")
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_constraint(KIND_CONSTRAINT, type_="check")
        batch.create_check_constraint(KIND_CONSTRAINT, KIND_BEFORE)
