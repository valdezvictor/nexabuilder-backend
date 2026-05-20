"""add bid_invitations and contractor_agreements tables

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-05-20
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = 'f6a7b8c9d0e1'
down_revision = 'e5f6a7b8c9d0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── bid_invitations ──────────────────────────────────────────────────────
    # One record per contractor per lead — tracks the full bid lifecycle
    op.create_table("bid_invitations",
        sa.Column("id",              sa.Integer,        primary_key=True, autoincrement=True),
        sa.Column("lead_id",         sa.Integer,        sa.ForeignKey("leads.id"), nullable=False, index=True),
        sa.Column("contractor_id",   sa.Integer,        sa.ForeignKey("contractors.id"), nullable=False, index=True),
        sa.Column("account_id",      sa.Integer,        nullable=True, index=True),  # contractor_accounts.id
        sa.Column("status",          sa.String(30),     nullable=False, default="pending"),
        # pending | sent | viewed | accepted | declined | expired | withdrawn
        sa.Column("sent_at",         sa.DateTime(timezone=True), nullable=True),
        sa.Column("viewed_at",       sa.DateTime(timezone=True), nullable=True),
        sa.Column("responded_at",    sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at",      sa.DateTime(timezone=True), nullable=True),
        sa.Column("decline_reason",  sa.Text,           nullable=True),
        sa.Column("bid_amount",      sa.Numeric(12,2),  nullable=True),  # contractor's bid
        sa.Column("bid_notes",       sa.Text,           nullable=True),
        sa.Column("commission_pct",  sa.Numeric(5,2),   nullable=True, default=10.0),  # NexaBuilder %
        sa.Column("created_at",      sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at",      sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_bid_inv_lead_contractor", "bid_invitations", ["lead_id", "contractor_id"], unique=True)

    # ── contractor_agreements ────────────────────────────────────────────────
    # Legal agreement signed by contractor on first portal access
    # Mario Trujillo to review before go-live
    op.create_table("contractor_agreements",
        sa.Column("id",                    sa.Integer,     primary_key=True, autoincrement=True),
        sa.Column("contractor_account_id", sa.Integer,     nullable=False, index=True),
        sa.Column("user_id",               UUID(as_uuid=False), nullable=False, index=True),
        sa.Column("agreement_version",     sa.String(20),  nullable=False),  # e.g. "1.0"
        sa.Column("agreed_at",             sa.DateTime(timezone=True), nullable=False),
        sa.Column("ip_address",            sa.String(45),  nullable=True),
        sa.Column("user_agent",            sa.Text,        nullable=True),
        sa.Column("full_name_signed",      sa.String(255), nullable=False),  # typed full name = signature
        sa.Column("email_signed",          sa.String(255), nullable=False),
        sa.Column("license_number",        sa.String(100), nullable=False),
        # Key terms acknowledged (stored for audit)
        sa.Column("terms_acknowledged",    JSONB,          nullable=True),
        # attorney_reviewed: set to true once Mario signs off
        sa.Column("attorney_reviewed",     sa.Boolean,     nullable=False, default=False),
        sa.Column("created_at",            sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("contractor_agreements")
    op.drop_index("ix_bid_inv_lead_contractor", "bid_invitations")
    op.drop_table("bid_invitations")
