"""When the Message-ID check last looked at a mailbox (#510).

Arming a mailbox to send needs ``message_id_verified_at``: a search by Message-ID
that found one of the app's own drafts there. Until one does, the Settings page and
``gmail status`` show why arming waits. ``mailboxes`` gains one column:

- ``message_id_checked_at``: when the drafts check last searched Gmail for one of
  the mailbox's drafts by its Message-ID and got an answer, found or not. A search
  that failed records nothing. ``serve`` writes it, so a separate process (the CLI)
  can say when the last check was.

No existing data changes. A mailbox from before this migration has no check
recorded, which reads as "not checked yet" until the next drafts check.

Revision ID: 0043
Revises: 0042
Create Date: 2026-10-14 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE: Final = "mailboxes"
COLUMN: Final = "message_id_checked_at"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column(COLUMN, sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_column(COLUMN)
