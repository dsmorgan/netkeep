"""Record which path wrote a headline and the last card headline seen (#557).

The profile's top-card headline wins over the connection card's unless the card's
own text changed. Two nullable columns on ``contacts`` carry what that rule needs:

- ``headline_via``: ``card`` or ``profile``, the path that last wrote ``headline``.
- ``card_headline_seen``: the last connection-card headline the sync saw.

Existing rows hold NULL in both, which the rule reads as "no profile headline to
protect", so nothing changes for them until an enrichment writes one.

Revision ID: 0045
Revises: 0044
Create Date: 2026-10-16 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("contacts") as batch:
        batch.add_column(sa.Column("headline_via", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("card_headline_seen", sa.String(length=500), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("contacts") as batch:
        batch.drop_column("card_headline_seen")
        batch.drop_column("headline_via")
