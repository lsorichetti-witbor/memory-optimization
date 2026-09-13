"""Order the embedding queue by when the memory was MADE, not when it arrived

Revision ID: 008
Revises: 007
Create Date: 2026-09-13

`created_at` on the queue row is the moment the server accepted it, which is not
the moment the memory was written. A client that spooled writes during an outage
replays them hours later, so every one of them arrives "now" and sorts after
memories that were made long after them. Recency is a ranking signal and the
queue drains oldest-first, so both were using the wrong clock.

`source_created_at` carries the memory's own timestamp, falling back to arrival
time when the caller did not send one.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "pending_memories",
        sa.Column("source_created_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Existing rows have no better answer than when they arrived, and leaving
    # them null would sort them unpredictably against rows that do have one.
    op.execute("UPDATE pending_memories SET source_created_at = created_at WHERE source_created_at IS NULL")

    # The claim query orders by this, so it belongs in the claimable index -
    # otherwise every pass sorts the whole queue, and a quota outage can enqueue
    # thousands of rows.
    op.drop_index("ix_pending_memories_claimable", table_name="pending_memories")
    op.create_index(
        "ix_pending_memories_claimable",
        "pending_memories",
        ["state", "next_attempt_at", "source_created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_pending_memories_claimable", table_name="pending_memories")
    op.create_index(
        "ix_pending_memories_claimable", "pending_memories", ["state", "next_attempt_at"]
    )
    op.drop_column("pending_memories", "source_created_at")
