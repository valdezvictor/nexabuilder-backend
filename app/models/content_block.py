import enum
from sqlalchemy import Column, String, Text, Boolean, DateTime, Integer, Enum, Index
from sqlalchemy.sql import func
from app.db import Base


class ContentType(enum.Enum):
    text      = "text"       # plain text / HTML copy
    image_url = "image_url"  # URL to image (swaps SVG placeholder)
    json      = "json"       # structured JSON (e.g. stats array)
    html      = "html"       # rich HTML block


class ContentBlock(Base):
    """
    Headless CMS content block.

    Primary key: (tenant_id, page_slug, block_key) — the three-part address
    used by both the site pages and the admin editor.

    page_slug   — matches the URL path: "/", "/services/pool-installation/",
                  "/locations/orange-county/irvine/", "/materials/stone/"
    block_key   — matches the data-cms-key attribute on the placeholder element
                  e.g. "hero_image", "about_team_photo", "mat_stone_0"
    tenant_id   — "nexabuilder" | "unapiscina" | "fitzhauer" | etc.
    content_type— what the value contains (text, image_url, json, html)
    value       — the actual content
    is_published— draft/live toggle — GET public endpoint only returns published
    version     — incremented on every PUT for simple audit trail
    """
    __tablename__ = "content_blocks"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id    = Column(String(100), nullable=False, default="nexabuilder", index=True)
    page_slug    = Column(String(500), nullable=False, index=True)
    block_key    = Column(String(200), nullable=False)
    content_type = Column(Enum(ContentType), nullable=False, default=ContentType.text)
    value        = Column(Text, nullable=True)
    alt_text     = Column(String(500), nullable=True)   # for image_url blocks
    is_published = Column(Boolean, nullable=False, default=False)
    version      = Column(Integer, nullable=False, default=1)
    created_at   = Column(DateTime(timezone=True), server_default=func.now())
    updated_at   = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    updated_by   = Column(String(200), nullable=True)   # email of last editor

    __table_args__ = (
        # Unique constraint: one block per (tenant, page, key)
        Index("ix_content_blocks_address", "tenant_id", "page_slug", "block_key", unique=True),
    )
