"""add property_assessments + active_projects tables

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-05-18
"""
from alembic import op
import sqlalchemy as sa

revision = 'c3d4e5f6a7b8'
down_revision = 'b2c3d4e5f6a7'
branch_labels = None
depends_on = None


def upgrade():
    # ── property_assessments ─────────────────────────────────────────────────
    # One canonical record per normalized property address.
    # Gates: one assessment per property per rolling 90-day window.
    op.create_table(
        "property_assessments",
        sa.Column("id",               sa.Integer(),    primary_key=True, autoincrement=True),
        sa.Column("address_hash",     sa.String(64),   nullable=False, index=True),
        # SHA-256 of normalized address — the dedup key
        sa.Column("address_raw",      sa.String(500),  nullable=False),
        # Original as submitted — for display / audit
        sa.Column("address_line1",    sa.String(255),  nullable=True),
        sa.Column("city",             sa.String(100),  nullable=True),
        sa.Column("state",            sa.String(2),    nullable=True),
        sa.Column("postal_code",      sa.String(10),   nullable=True),
        sa.Column("user_id",          sa.dialects.postgresql.UUID(as_uuid=False), nullable=False, index=True),
        sa.Column("lead_id",          sa.Integer(),    nullable=False, index=True),
        sa.Column("vertical",         sa.String(100),  nullable=True),
        sa.Column("permit_verified",  sa.Boolean(),    nullable=False, server_default="false"),
        # True when address was confirmed against permit DB or county records
        sa.Column("homeowner_verified", sa.Boolean(),  nullable=False, server_default="false"),
        # True when user confirmed they own/rent this address
        sa.Column("assessment_count", sa.Integer(),    nullable=False, server_default="1"),
        # Incremented if same property gets another assessment (audit trail)
        sa.Column("first_assessed_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("last_assessed_at",  sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    # Unique: one record per (address_hash, user_id) — same owner, same house = update not insert
    op.create_index(
        "ix_property_assessments_hash_user",
        "property_assessments", ["address_hash", "user_id"], unique=True
    )
    # Query index: is this address already assessed (by anyone) in the last 90 days?
    op.create_index(
        "ix_property_assessments_hash_date",
        "property_assessments", ["address_hash", "last_assessed_at"]
    )

    # ── active_projects ──────────────────────────────────────────────────────
    # Links a verified contractor to an active project at a specific property.
    # A contractor can only run an assessment if an active_project record
    # exists with their license_number + matching address_hash + status active.
    op.create_table(
        "active_projects",
        sa.Column("id",                  sa.Integer(),   primary_key=True, autoincrement=True),
        sa.Column("license_number",      sa.String(100), nullable=False, index=True),
        sa.Column("state_code",          sa.String(2),   nullable=False, server_default="CA"),
        sa.Column("address_hash",        sa.String(64),  nullable=False, index=True),
        sa.Column("address_line1",       sa.String(255), nullable=True),
        sa.Column("city",                sa.String(100), nullable=True),
        sa.Column("state",               sa.String(2),   nullable=True),
        sa.Column("postal_code",         sa.String(10),  nullable=True),
        sa.Column("lead_id",             sa.Integer(),   nullable=True, index=True),
        # Populated when lead is formally matched to this contractor
        sa.Column("vertical",            sa.String(100), nullable=True),
        sa.Column("project_status",      sa.String(30),  nullable=False, server_default="active"),
        # active | completed | cancelled | on_hold
        sa.Column("source",              sa.String(50),  nullable=True),
        # nexabuilder_lead | contractor_added | permit_import
        sa.Column("permit_number",       sa.String(100), nullable=True),
        # If sourced from permit data
        sa.Column("assessment_count",    sa.Integer(),   nullable=False, server_default="0"),
        sa.Column("last_assessment_at",  sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at",          sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at",          sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    # A contractor can have one active record per property address
    op.create_index(
        "ix_active_projects_license_hash",
        "active_projects", ["license_number", "address_hash"], unique=True
    )


def downgrade():
    op.drop_index("ix_active_projects_license_hash", table_name="active_projects")
    op.drop_table("active_projects")
    op.drop_index("ix_property_assessments_hash_date", table_name="property_assessments")
    op.drop_index("ix_property_assessments_hash_user", table_name="property_assessments")
    op.drop_table("property_assessments")
