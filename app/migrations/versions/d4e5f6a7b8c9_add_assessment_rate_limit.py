"""add assessment_rate_limit table

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-05-18
"""
from alembic import op
import sqlalchemy as sa

revision = 'd4e5f6a7b8c9'
down_revision = 'c3d4e5f6a7b8'
branch_labels = None
depends_on = None


def upgrade():
    # Tracks assessment attempts per user per rolling 1-hour window.
    # Only counted when the address is already in property_assessments (has a lead).
    # Anonymous / new-address attempts don't count — the property gate handles those.
    op.create_table(
        "assessment_rate_log",
        sa.Column("id",           sa.Integer(),  primary_key=True, autoincrement=True),
        sa.Column("user_id",      sa.String(36), nullable=False, index=True),
        sa.Column("address_hash", sa.String(64), nullable=False),
        sa.Column("lead_id",      sa.Integer(),  nullable=True),
        # lead_id from the matched property_assessment record
        sa.Column("attempted_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), index=True),
    )
    # Fast lookup: how many attempts has this user made in the last hour?
    op.create_index(
        "ix_rate_log_user_time",
        "assessment_rate_log", ["user_id", "attempted_at"]
    )


def downgrade():
    op.drop_index("ix_rate_log_user_time", table_name="assessment_rate_log")
    op.drop_table("assessment_rate_log")
