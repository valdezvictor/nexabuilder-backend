"""add content_blocks CMS table

Revision ID: a1b2c3d4e5f6
Revises: dfcba14c573f
Create Date: 2026-05-18 12:08:45

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'dfcba14c573f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "content_blocks",
        sa.Column("id",           sa.Integer(),                       primary_key=True, autoincrement=True),
        sa.Column("tenant_id",    sa.String(100),  nullable=False,    server_default="nexabuilder"),
        sa.Column("page_slug",    sa.String(500),  nullable=False),
        sa.Column("block_key",    sa.String(200),  nullable=False),
        sa.Column("content_type", sa.Enum("text","image_url","json","html", name="contenttype"),
                                                    nullable=False,    server_default="text"),
        sa.Column("value",        sa.Text(),       nullable=True),
        sa.Column("alt_text",     sa.String(500),  nullable=True),
        sa.Column("is_published", sa.Boolean(),    nullable=False,    server_default="false"),
        sa.Column("version",      sa.Integer(),    nullable=False,    server_default="1"),
        sa.Column("updated_by",   sa.String(200),  nullable=True),
        sa.Column("created_at",   sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at",   sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )

    # Unique index: one block per (tenant, page, key)
    op.create_index(
        "ix_content_blocks_address",
        "content_blocks",
        ["tenant_id", "page_slug", "block_key"],
        unique=True,
    )

    # Performance indexes
    op.create_index("ix_content_blocks_tenant_id",    "content_blocks", ["tenant_id"])
    op.create_index("ix_content_blocks_page_slug",    "content_blocks", ["page_slug"])
    op.create_index("ix_content_blocks_is_published", "content_blocks", ["is_published"])


def downgrade() -> None:
    op.drop_index("ix_content_blocks_is_published", table_name="content_blocks")
    op.drop_index("ix_content_blocks_page_slug",    table_name="content_blocks")
    op.drop_index("ix_content_blocks_tenant_id",    table_name="content_blocks")
    op.drop_index("ix_content_blocks_address",      table_name="content_blocks")
    op.drop_table("content_blocks")
    op.execute("DROP TYPE IF EXISTS contenttype")
