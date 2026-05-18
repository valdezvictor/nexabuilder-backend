"""add verification gate — otp_codes + contractor_accounts + user/lead flags

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-05-18
"""
from alembic import op
import sqlalchemy as sa

revision = 'b2c3d4e5f6a7'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    # ── otp_codes — short-lived 6-digit codes for email/SMS verification ──
    op.create_table(
        "otp_codes",
        sa.Column("id",           sa.Integer(),     primary_key=True, autoincrement=True),
        sa.Column("user_id",      sa.dialects.postgresql.UUID(as_uuid=False), nullable=False, index=True),
        sa.Column("code",         sa.String(6),     nullable=False),
        sa.Column("channel",      sa.String(10),    nullable=False),  # email | sms
        sa.Column("purpose",      sa.String(30),    nullable=False),  # verification | login
        sa.Column("is_used",      sa.Boolean(),     nullable=False, server_default="false"),
        sa.Column("attempts",     sa.Integer(),     nullable=False, server_default="0"),
        sa.Column("expires_at",   sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at",   sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_otp_codes_user_channel", "otp_codes", ["user_id", "channel", "is_used"])

    # ── contractor_accounts — CSLB-verified contractor portal accounts ──
    op.create_table(
        "contractor_accounts",
        sa.Column("id",                    sa.Integer(),  primary_key=True, autoincrement=True),
        sa.Column("user_id",               sa.dialects.postgresql.UUID(as_uuid=False), nullable=False, unique=True, index=True),
        sa.Column("license_number",        sa.String(100), nullable=False, index=True),
        sa.Column("state_code",            sa.String(2),   nullable=False, server_default="CA"),
        sa.Column("cslb_verified",         sa.Boolean(),   nullable=False, server_default="false"),
        sa.Column("challenge_status",      sa.String(30),  nullable=False, server_default="pending"),
        # pending | passed | failed | locked
        sa.Column("challenge_attempts",    sa.Integer(),   nullable=False, server_default="0"),
        sa.Column("challenge_passed_at",   sa.DateTime(timezone=True), nullable=True),
        sa.Column("contractor_db_id",      sa.Integer(),   nullable=True),  # FK to contractors.id when matched
        sa.Column("company_name",          sa.String(255), nullable=True),
        sa.Column("created_at",            sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at",            sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )

    # ── Add verification columns to users ──
    op.add_column("users", sa.Column("is_email_verified", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("users", sa.Column("is_phone_verified", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("users", sa.Column("verification_method", sa.String(30), nullable=True))
    # email_otp | sms_otp | cslb_challenge | legacy

    # ── Add assessment_released to leads ──
    op.add_column("leads", sa.Column("assessment_released", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("leads", sa.Column("user_id", sa.dialects.postgresql.UUID(as_uuid=False), nullable=True, index=True))


def downgrade():
    op.drop_column("leads", "user_id")
    op.drop_column("leads", "assessment_released")
    op.drop_column("users", "verification_method")
    op.drop_column("users", "is_phone_verified")
    op.drop_column("users", "is_email_verified")
    op.drop_index("ix_otp_codes_user_channel", table_name="otp_codes")
    op.drop_table("contractor_accounts")
    op.drop_table("otp_codes")
