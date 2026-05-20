"""add project milestones and escrow transaction tables

Revision ID: a1b2c3d4e5f6
Revises: f6a7b8c9d0e1
Create Date: 2026-05-20
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = 'a1b2c3d4e5f6'
down_revision = 'f6a7b8c9d0e1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── project_milestones ───────────────────────────────────────────────────
    # One row per project phase — both parties must confirm before escrow releases
    op.create_table("project_milestones",
        sa.Column("id",                sa.Integer,        primary_key=True, autoincrement=True),
        sa.Column("lead_id",           sa.Integer,        sa.ForeignKey("leads.id"), nullable=False, index=True),
        sa.Column("bid_id",            sa.Integer,        nullable=True, index=True),
        sa.Column("milestone_number",  sa.Integer,        nullable=False),  # 1, 2, 3...
        sa.Column("title",             sa.String(200),    nullable=False),   # "Excavation & Gunite"
        sa.Column("description",       sa.Text,           nullable=True),
        sa.Column("phase_amount",      sa.Numeric(12,2),  nullable=False),   # $ for this phase
        sa.Column("nexabuilder_fee",   sa.Numeric(12,2),  nullable=True),    # our commission
        sa.Column("status",            sa.String(30),     nullable=False, default="pending"),
        # pending | contractor_confirmed | homeowner_confirmed | both_confirmed | released | disputed
        sa.Column("contractor_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("homeowner_confirmed_at",  sa.DateTime(timezone=True), nullable=True),
        sa.Column("escrow_release_at",       sa.DateTime(timezone=True), nullable=True),
        sa.Column("contractor_notes",  sa.Text,           nullable=True),
        sa.Column("homeowner_notes",   sa.Text,           nullable=True),
        sa.Column("dispute_reason",    sa.Text,           nullable=True),
        sa.Column("created_at",        sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at",        sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # ── escrow_transactions ───────────────────────────────────────────────────
    # Audit trail of every escrow event — sent to Raul's system
    op.create_table("escrow_transactions",
        sa.Column("id",              sa.Integer,        primary_key=True, autoincrement=True),
        sa.Column("lead_id",         sa.Integer,        sa.ForeignKey("leads.id"), nullable=False),
        sa.Column("milestone_id",    sa.Integer,        nullable=True),
        sa.Column("transaction_type", sa.String(50),   nullable=False),
        # funded | phase_released | commission_collected | refunded | disputed
        sa.Column("amount",          sa.Numeric(12,2),  nullable=False),
        sa.Column("fee_amount",      sa.Numeric(12,2),  nullable=True),    # NexaBuilder cut
        sa.Column("status",          sa.String(30),     nullable=False, default="pending"),
        # pending | processing | completed | failed | reversed
        sa.Column("escrow_ref",      sa.String(200),    nullable=True),    # Raul's escrow ref #
        sa.Column("lender_ref",      sa.String(200),    nullable=True),    # LendAPI loan ref
        sa.Column("payload",         JSONB,             nullable=True),    # full event data
        sa.Column("created_at",      sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("escrow_transactions")
    op.drop_table("project_milestones")
