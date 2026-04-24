"""add routing fields + routing_events table

Revision ID: dfcba14c573f
Revises: ad4c625e8b9e
Create Date: 2026-04-02 16:40:05.416101

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'dfcba14c573f'
down_revision: Union[str, None] = 'ad4c625e8b9e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    # Add new contractor fields
    op.add_column("contractors", sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"))
    op.add_column("contractors", sa.Column("daily_capacity", sa.Integer(), nullable=True))
    op.add_column("contractors", sa.Column("active_leads_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("contractors", sa.Column("performance_score", sa.Float(), nullable=True))
    op.add_column("contractors", sa.Column("last_assigned_at", sa.DateTime(timezone=True), nullable=True))

    # Create routing_events table
    op.create_table(
        "routing_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("lead_id", sa.Integer(), sa.ForeignKey("leads.id"), nullable=False),
        sa.Column("contractor_id", sa.Integer(), sa.ForeignKey("contractors.id"), nullable=True),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade():
    op.drop_table("routing_events")
    op.drop_column("contractors", "last_assigned_at")
    op.drop_column("contractors", "performance_score")
    op.drop_column("contractors", "active_leads_count")
    op.drop_column("contractors", "daily_capacity")
    op.drop_column("contractors", "is_active")
