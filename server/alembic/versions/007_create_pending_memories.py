"""Create pending_memories: the server-side embedding queue

Revision ID: 007
Revises: 006
Create Date: 2026-09-13

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pending_memories",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.String(64), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    # A client retry after a timeout must be a no-op, not a second copy.
    op.create_unique_constraint(
        "uq_pending_memories_idempotency_key", "pending_memories", ["idempotency_key"]
    )
    # The claim query's WHERE clause, so picking up work stays cheap as the
    # queue grows - a quota outage can enqueue thousands of rows.
    op.create_index(
        "ix_pending_memories_claimable",
        "pending_memories",
        ["state", "next_attempt_at"],
    )
    # Embedding reuse looks up by content hash.
    op.create_index("ix_pending_memories_content_hash", "pending_memories", ["content_hash"])


def downgrade() -> None:
    op.drop_index("ix_pending_memories_content_hash", table_name="pending_memories")
    op.drop_index("ix_pending_memories_claimable", table_name="pending_memories")
    op.drop_constraint("uq_pending_memories_idempotency_key", "pending_memories", type_="unique")
    op.drop_table("pending_memories")
