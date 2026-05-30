"""add blog_articles table

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-05-30
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE TYPE articlestatus AS ENUM "
               "('draft','review','scheduled','published','archived')")

    op.create_table(
        "blog_articles",
        # Identity & Routing
        sa.Column("id",                   sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("site_id",              sa.String(100),  nullable=False),
        sa.Column("slug",                 sa.String(300),  nullable=False),
        sa.Column("language",             sa.String(10),   nullable=False, server_default="es"),
        sa.Column("hreflang_pair_slug",   sa.String(300),  nullable=True),
        # SEO Core
        sa.Column("h1",                   sa.String(200),  nullable=False),
        sa.Column("seo_title",            sa.String(120),  nullable=False),
        sa.Column("meta_description",     sa.String(320),  nullable=False),
        sa.Column("canonical_url",        sa.String(500),  nullable=True),
        sa.Column("primary_keyword",      sa.String(200),  nullable=True),
        # Content
        sa.Column("deck",                 sa.Text(),       nullable=True),
        sa.Column("body_html",            sa.Text(),       nullable=True),
        sa.Column("toc_auto",             sa.Boolean(),    nullable=False, server_default="true"),
        # Media
        sa.Column("featured_image_url",   sa.String(500),  nullable=True),
        sa.Column("featured_image_alt",   sa.String(300),  nullable=True),
        sa.Column("featured_image_caption", sa.String(500), nullable=True),
        sa.Column("og_image_url",         sa.String(500),  nullable=True),
        # Taxonomy
        sa.Column("category",             sa.String(100),  nullable=True),
        sa.Column("tags",                 sa.Text(),       nullable=True),
        sa.Column("related_article_ids",  sa.Text(),       nullable=True),
        # Authorship
        sa.Column("author_name",          sa.String(200),  nullable=False,
                  server_default="Equipo Una Piscina"),
        sa.Column("author_bio",           sa.Text(),       nullable=True),
        sa.Column("author_avatar",        sa.String(500),  nullable=True),
        # Publishing
        sa.Column("status",               sa.Enum("draft","review","scheduled",
                                                  "published","archived",
                                                  name="articlestatus"),
                  nullable=False, server_default="draft"),
        sa.Column("published_at",         sa.DateTime(timezone=True), nullable=True),
        sa.Column("scheduled_at",         sa.DateTime(timezone=True), nullable=True),
        sa.Column("modified_at",          sa.DateTime(timezone=True),
                  server_default=sa.text("now()")),
        sa.Column("created_at",           sa.DateTime(timezone=True),
                  server_default=sa.text("now()")),
        sa.Column("created_by",           sa.String(200),  nullable=True),
        sa.Column("updated_by",           sa.String(200),  nullable=True),
        # Schema / SEO
        sa.Column("schema_json",          sa.Text(),       nullable=True),
        sa.Column("schema_types",         sa.Text(),       nullable=True),
        # Local SEO
        sa.Column("geo_region",           sa.String(100),  nullable=True),
        sa.Column("geo_cities",           sa.Text(),       nullable=True),
        # AEO / GEO
        sa.Column("aeo_target_questions", sa.Text(),       nullable=True),
        # OG overrides
        sa.Column("og_title",             sa.String(200),  nullable=True),
        sa.Column("og_description",       sa.String(320),  nullable=True),
        # Analytics
        sa.Column("reading_time_minutes", sa.Integer(),    nullable=True),
        sa.Column("word_count",           sa.Integer(),    nullable=True),
    )

    # Unique routing constraint
    op.create_index("ix_blog_articles_routing",
                    "blog_articles", ["site_id", "slug", "language"], unique=True)
    # Performance indexes
    op.create_index("ix_blog_articles_site_status",
                    "blog_articles", ["site_id", "status"])
    op.create_index("ix_blog_articles_published_at",
                    "blog_articles", ["published_at"])
    op.create_index("ix_blog_articles_category",
                    "blog_articles", ["site_id", "category"])
    op.create_index("ix_blog_articles_site_id",
                    "blog_articles", ["site_id"])


def downgrade() -> None:
    op.drop_index("ix_blog_articles_category",    table_name="blog_articles")
    op.drop_index("ix_blog_articles_published_at", table_name="blog_articles")
    op.drop_index("ix_blog_articles_site_status", table_name="blog_articles")
    op.drop_index("ix_blog_articles_routing",     table_name="blog_articles")
    op.drop_index("ix_blog_articles_site_id",     table_name="blog_articles")
    op.drop_table("blog_articles")
    op.execute("DROP TYPE IF EXISTS articlestatus")
