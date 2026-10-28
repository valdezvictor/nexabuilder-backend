"""
app/models/blog_article.py
===========================
Blog article model — designed from 3 concrete unapiscina.com articles.
Every field maps to something actually used. No speculative fields.
"""
import enum
from sqlalchemy import Column, String, Text, Boolean, DateTime, Integer, Enum, Index
from sqlalchemy.sql import func
from app.db import Base


class ArticleStatus(enum.Enum):
    draft     = "draft"
    review    = "review"
    scheduled = "scheduled"
    published = "published"
    archived  = "archived"


class BlogArticle(Base):
    """
    Full blog article with SEO, AEO/GEO, local, and publishing metadata.

    Primary routing:  site_id + slug + language
    Public URL:       /blog/{slug}/  (ES) or /en/blog/{slug}/ (EN)
    """
    __tablename__ = "blog_articles"

    # Identity & Routing
    id                   = Column(Integer, primary_key=True, autoincrement=True)
    site_id              = Column(String(100), nullable=False, index=True)
    slug                 = Column(String(300), nullable=False)
    language             = Column(String(10),  nullable=False, default="es")
    hreflang_pair_slug   = Column(String(300), nullable=True)

    # SEO Core
    h1                   = Column(String(200), nullable=False)
    seo_title            = Column(String(120), nullable=False)
    meta_description     = Column(String(320), nullable=False)
    canonical_url        = Column(String(500), nullable=True)
    primary_keyword      = Column(String(200), nullable=True)

    # Content
    deck                 = Column(Text, nullable=True)
    body_html            = Column(Text, nullable=True)
    toc_auto             = Column(Boolean, nullable=False, default=True)

    # Media
    featured_image_url   = Column(String(500), nullable=True)
    featured_image_alt   = Column(String(300), nullable=True)
    featured_image_caption = Column(String(500), nullable=True)
    og_image_url         = Column(String(500), nullable=True)

    # Taxonomy
    category             = Column(String(100), nullable=True)
    tags                 = Column(Text, nullable=True)  # comma-separated
    related_article_ids  = Column(Text, nullable=True)  # comma-separated IDs

    # Authorship
    author_name          = Column(String(200), nullable=False, default="Equipo Una Piscina")
    author_bio           = Column(Text, nullable=True)
    author_avatar        = Column(String(500), nullable=True)

    # Publishing Workflow
    status               = Column(Enum(ArticleStatus, name="articlestatus"),
                                  nullable=False, default=ArticleStatus.draft)
    published_at         = Column(DateTime(timezone=True), nullable=True)
    scheduled_at         = Column(DateTime(timezone=True), nullable=True)
    modified_at          = Column(DateTime(timezone=True),
                                  server_default=func.now(), onupdate=func.now())
    created_at           = Column(DateTime(timezone=True), server_default=func.now())
    created_by           = Column(String(200), nullable=True)
    updated_by           = Column(String(200), nullable=True)

    # Schema / SEO
    schema_json          = Column(Text, nullable=True)   # full JSON-LD string for <head>
    schema_types         = Column(Text, nullable=True)   # comma: "Article,FAQPage,LocalBusiness"

    # Local SEO
    geo_region           = Column(String(100), nullable=True)
    geo_cities           = Column(Text, nullable=True)   # comma-separated cities

    # AEO / GEO
    aeo_target_questions = Column(Text, nullable=True)   # pipe-separated questions

    # OG overrides (fall back to seo_title / meta_description if null)
    og_title             = Column(String(200), nullable=True)
    og_description       = Column(String(320), nullable=True)

    # Analytics
    reading_time_minutes = Column(Integer, nullable=True)  # auto: word_count / 200
    word_count           = Column(Integer, nullable=True)  # auto on save

    __table_args__ = (
        Index("ix_blog_articles_routing",
              "site_id", "slug", "language", unique=True),
        Index("ix_blog_articles_site_status", "site_id", "status"),
        Index("ix_blog_articles_published_at", "published_at"),
        Index("ix_blog_articles_category", "site_id", "category"),
    )
